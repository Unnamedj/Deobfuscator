"""
ZEUS script model: finds the pieces of the obfuscator's runtime in the AST.

The script is one wrapper function whose locals hold a handful of helpers and
one giant dispatcher `F(state, args, cells, gc)`: a `while state do` loop over
an if-tree that compares the state with integer constants. Every leaf of the
tree is a straight-line block (a register machine over ~100 locals of the
dispatcher) that ends by assigning the next state to `state`; a nil state ends
the call. A Lua function of the original script is a set of leaves reached
from an entry state; closures are made by `maker(entry state, {cells})`,
captured variables live in cells `w[cell id]`, and the cells a closure
received are `N[i]`. Names are randomized per build, so every role is found
by the shape of its code (see Model).
"""
import json
import os
import re
import subprocess
import tempfile

import harness

INF = 1 << 62


class Leaf:
    __slots__ = ("idx", "lo", "hi", "stmts", "reads_state")

    def __init__(self, idx, lo, hi, stmts):
        self.idx, self.lo, self.hi, self.stmts = idx, lo, hi, stmts
        self.reads_state = False


class Maker:
    """A closure constructor: `maker(entry state, {cells})`."""
    __slots__ = ("arity",)      # None: variadic

    def __init__(self, arity):
        self.arity = arity

    def __repr__(self):
        return "Maker(%s)" % ("..." if self.arity is None else self.arity)


class Special:
    """A named runtime object of the wrapper (env, unpack, select, cell store, ...)."""
    __slots__ = ("kind",)

    def __init__(self, kind):
        self.kind = kind

    def __repr__(self):
        return "Special(%s)" % self.kind


class ModelError(Exception):
    pass


def parse_ast(path):
    out = subprocess.run([harness.luau_ast(), path], capture_output=True)
    text = out.stdout.decode("latin-1")
    start = text.find('{"root"')
    if start < 0:
        raise ModelError("luau-ast could not parse the script: %s" % text[:200].strip())
    return json.loads(text[start:])["root"]


def parse_source(text):
    """parse_ast of in-memory text (detection: no input file exists yet)."""
    fd, path = tempfile.mkstemp(suffix=".lua", prefix="zdetect_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode("latin-1") if isinstance(text, str) else text)
        return parse_ast(path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _loc(n):
    m = re.match(r"(\d+),(\d+) - (\d+),(\d+)", n["location"])
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))


def walk(n, f):
    stack = [n]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            if "type" in x:
                f(x)
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)


def functions_in(n):
    out = []
    walk(n, lambda x: out.append(x) if x["type"] == "AstExprFunction" else None)
    return out


class Model:
    """What the lifter needs to know about the script.

    roles    {declaration location of a wrapper local: Special | Maker}
    regs     {declaration location of a dispatcher local or parameter: index}
    leaves   dispatcher leaves sorted by state interval
    entry    state of the main function
    consts   the dispatcher's constant table (`local k = {[n] = number}`)
    """

    def __init__(self, root, source):
        self.root = root
        self.source = source
        self.expr_requests = {}     # call node location -> source text (decoder calls with constant args)
        self.expr_resolved = {}     # call node location -> value, see decoders.resolve_exprs
        self.outer_requests = {}    # declaration location -> {arg tuple, ...}, filled while lifting
        self.outer_resolved = {}    # declaration location -> {arg tuple: value}, see decoders.py
        self.find_wrapper()
        self.collect_outer_locals()
        self.find_roles()
        self.find_dispatcher()
        self.build_leaves()

    # ---- wrapper: `return (function(<params>) <setup> end)(newproxy, select, env, ...)`
    MAX_DECODER_LAYERS = 6

    def find_wrapper(self):
        """`return (function(<params>) <setup> ... end)(<args>)`. Some builds add outer layers
        in front of it that only decode the call's own argument names/strings (a local string
        table + a custom-base64 loop) and end in another `return (f)(...)`: follow those until
        a function whose first statement is the setup assignment turns up. The layers passed
        through are kept (`self.decoder_layers`): some of their locals (a decode-by-index
        function, typically) are referenced from inside the dispatcher's leaves too."""
        layers = []
        stmt, call = self._last_return_call(self.root["body"])
        for _ in range(self.MAX_DECODER_LAYERS):
            if call is None:
                raise ModelError("no `return (function(...) ... end)(...)` wrapper")
            fn = call["func"]
            while fn["type"] == "AstExprGroup":
                fn = fn["expr"]
            if fn["type"] != "AstExprFunction":
                raise ModelError("wrapper is not a function")
            body = fn["body"]["body"]
            if body and body[0]["type"] == "AstStatAssign" and len(body[0]["vars"]) >= 8:
                self.wrapper, self.wrapper_call, self.decoder_layers = fn, call, layers
                return
            nxt_stmt, nxt_call = self._last_return_call(body)   # a decoder layer: follow its own call
            layers.append((fn, nxt_stmt))
            stmt, call = nxt_stmt, nxt_call
        raise ModelError("no setup assignment found through %d decoder layers" % self.MAX_DECODER_LAYERS)

    @staticmethod
    def _last_return_call(stmts):
        for s in reversed(stmts):
            if s["type"] == "AstStatReturn" and len(s["list"]) == 1 and s["list"][0]["type"] == "AstExprCall":
                return s, s["list"][0]
        return None, None

    def collect_outer_locals(self):
        """Declarations made by the decoder layers (find_wrapper), outside the wrapper's own
        scope: the dispatcher's leaves can still reference them as Lua upvalues (a decode-by-
        index function, typically). {declaration location: name}."""
        self.outer_locals = {}

        def declares(n, out):
            t = n["type"]
            if t == "AstStatLocal":
                for v in n["vars"]:
                    out[v["location"]] = v["name"]
            elif t == "AstStatLocalFunction":
                out[n["name"]["location"]] = n["name"]["name"]
            elif t == "AstStatForIn":
                for v in n["vars"]:
                    out[v["location"]] = v["name"]
            elif t == "AstStatFor":
                out[n["var"]["location"]] = n["var"]["name"]

        for fn, chain_stmt in self.decoder_layers:
            for s in fn["body"]["body"]:
                if s is chain_stmt:
                    continue           # the next layer / the real wrapper: handled separately
                walk(s, lambda n: declares(n, self.outer_locals))

        self.prescan_outer_calls()
        self.prescan_outer_exprs()

    ARITH = ("Add", "Sub", "Mul", "Div", "FloorDiv", "Mod", "Pow", "Concat")

    def prescan_outer_exprs(self):
        """The newer build writes every string as `iZC[k] or SZC(a5C(a, b), c)`: nested calls of
        decoder-layer locals whose arguments are all constants. Each such call, outermost first, is
        one request (its source text) answered by running the layers (decoders.resolve_exprs)."""
        covered = set()
        found = []

        def const_expr(n):
            t = n["type"]
            if t in ("AstExprConstantNumber", "AstExprConstantString", "AstExprConstantBool", "AstExprConstantNil"):
                return True
            if t == "AstExprGroup":
                return const_expr(n["expr"])
            if t == "AstExprUnary":
                return n["op"] == "Minus" and const_expr(n["expr"])
            if t == "AstExprBinary":
                return n["op"] in self.ARITH and const_expr(n["left"]) and const_expr(n["right"])
            if t == "AstExprCall" and n["func"]["type"] == "AstExprLocal" \
                    and n["func"]["local"]["location"] in self.outer_locals:
                if all(const_expr(a) for a in n["args"]):
                    covered.update(id(a) for a in n["args"])
                    found.append(n)
                    return True
            return False

        def visit(n):
            if n["type"] == "AstExprCall":
                const_expr(n)
        walk(self.root, visit)
        # a call nested in another request's arguments is not a request of its own
        inner = set()

        def mark(n):
            if n["type"] == "AstExprCall":
                for a in n["args"]:
                    walk(a, lambda x: inner.add(id(x)))
        for n in found:
            mark(n)
        for n in found:
            if id(n) not in inner:
                self.expr_requests[n["location"]] = self.text(n)

    def prescan_outer_calls(self):
        """Every call of an outer local (collect_outer_locals) anywhere in the file, with
        constant arguments: resolved once up front (decoders.py), so the lifter does not have to
        discover them one request at a time -- almost every one ends up needed immediately (it
        picks a library member or feeds a comparison the dispatch itself branches on)."""
        def visit(n):
            if n["type"] != "AstExprCall" or n["func"]["type"] != "AstExprLocal":
                return
            loc = n["func"]["local"]["location"]
            if loc not in self.outer_locals:
                return
            args = [const_eval(a) for a in n["args"]]
            if any(a is None for a in args):
                return
            self.outer_requests.setdefault(loc, set()).add(tuple(args))
        walk(self.root, visit)

    def text(self, n):
        l1, c1, l2, c2 = _loc(n)
        if not hasattr(self, "_starts"):
            self._starts = [0]
            for m in re.finditer("\n", self.source):
                self._starts.append(m.end())
        return self.source[self._starts[l1] + c1:self._starts[l2] + c2]

    def find_roles(self):
        w = self.wrapper
        params = {i: a for i, a in enumerate(w["args"])}
        roles = {}
        # positional arguments of the wrapper call: the runtime functions the script receives
        for i, arg in enumerate(self.wrapper_call["args"]):
            if i >= len(params):
                break
            t = self.text(arg)
            kind = None
            if t == "newproxy":
                kind = "newproxy"
            elif t == "select":
                kind = "select"
            elif "getfenv" in t or "_ENV" in t:
                kind = "env"
            elif "unpack" in t:
                kind = "unpack"
            elif t == "setmetatable":
                kind = "setmetatable"
            elif t == "getmetatable":
                kind = "getmetatable"
            elif t == "{...}":
                kind = "script_args"
            if kind:
                roles[params[i]["location"]] = Special(kind)
        # the setup assignment: `V, J, p, ... = function() ... end, function(o, t) ... end, {}, 0, ...`
        # (the newer build splits it in two, around a `do ... end` block that fills the constant table)
        setups = [s for s in w["body"]["body"]
                  if s["type"] == "AstStatAssign" and len(s["vars"]) >= 3
                  and all(v["type"] == "AstExprLocal" for v in s["vars"])]
        setup = None
        for s in w["body"]["body"]:
            if s["type"] == "AstStatAssign" and len(s["vars"]) >= 8:
                setup = s
        if setup is None:
            raise ModelError("no setup assignment in the wrapper")
        self.setup = setup
        locs = {a["name"]: a["location"] for a in w["args"]}
        self.wrapper_locals = locs
        disp_loc = None
        for st in setups:
            for v, val in zip(st["vars"], st["values"]):
                loc = v["local"]["location"]
                if val["type"] == "AstExprFunction":
                    kind = self.classify_function(val)
                    if kind == "dispatcher":
                        disp_loc = loc
                        self.dispatcher_fn = val
                        roles[loc] = Special("dispatcher")
                    elif isinstance(kind, Maker):
                        roles[loc] = kind
                        self.maker_fn = val
                    elif kind:
                        roles[loc] = Special(kind)
                elif val["type"] == "AstExprTable" and not val["items"]:
                    roles.setdefault(loc, Special("table"))
        if disp_loc is None:
            raise ModelError("no dispatcher function in the wrapper")
        self.setups = setups
        self.name_roles(setups, roles)
        self.disp_loc = disp_loc
        self.roles = roles
        # entry: `return (maker(<state>, {}[, <mask>]))(unpack(<script args>))`
        ret = [s for s in w["body"]["body"] if s["type"] == "AstStatReturn"]
        if not ret:
            raise ModelError("no entry call in the wrapper")
        call = ret[-1]["list"][0]
        f = call["func"]
        while f["type"] == "AstExprGroup":
            f = f["expr"]
        ev = const_eval(f["args"][0]) if f["type"] == "AstExprCall" and f["args"] else None
        if ev is None:
            raise ModelError("unexpected entry call %s" % self.text(call))
        self.entry = int(ev)
        self.entry_mask = None
        if len(f["args"]) >= 3:
            mv = const_eval(f["args"][2])
            self.entry_mask = None if mv is None else int(mv)
        self.entry_maker = roles.get(f["func"]["local"]["location"]) if f["func"]["type"] == "AstExprLocal" else None
        # the newer build unpacks the script arguments through a looked-up `unpack`
        if call["args"] and call["args"][0]["type"] == "AstExprCall" \
                and call["args"][0]["func"]["type"] == "AstExprLocal":
            loc = call["args"][0]["func"]["local"]["location"]
            if loc not in roles:
                roles[loc] = Special("unpack")

    def name_roles(self, setups, roles):
        """Of the wrapper's empty tables, the refcount table is the one the alloc function
        writes (`k[n] = 1`); the other holds the cells' values. The newer build has a third, the
        constant table: filled by the wrapper's own set-up code and only read by the dispatcher."""
        tables = [loc for loc, r in roles.items() if isinstance(r, Special) and r.kind == "table"]
        refc = None
        for setup in setups:
            for v, val in zip(setup["vars"], setup["values"]):
                r = roles.get(v["local"]["location"])
                if isinstance(r, Special) and r.kind == "alloc":
                    for s in val["body"]["body"]:
                        if s["type"] == "AstStatAssign" and s["vars"][0]["type"] == "AstExprIndexExpr":
                            e = s["vars"][0]["expr"]
                            if e["type"] == "AstExprLocal":
                                refc = e["local"]["location"]
        written = set()

        def writes(x):
            if x["type"] == "AstStatAssign":
                for t in x["vars"]:
                    if t["type"] == "AstExprIndexExpr" and t["expr"]["type"] == "AstExprLocal":
                        written.add(t["expr"]["local"]["location"])
        walk(self.dispatcher_fn["body"], writes)
        self.consts_table = None
        self.consts_name = None
        for loc in tables:
            if loc == refc:
                roles[loc] = Special("refcount")
            elif self.dispatcher_loop_kind() == "repeat" and loc not in written:
                roles[loc] = Special("consts")
                self.consts_table = loc
                self.consts_name = [a["name"] for a in self.wrapper["args"] if a["location"] == loc][0]
            else:
                roles[loc] = Special("cells")

    def dispatcher_loop_kind(self):
        for s in self.dispatcher_fn["body"]["body"]:
            if s["type"] == "AstStatRepeat":
                return "repeat"
            if s["type"] == "AstStatWhile":
                return "while"
        return None

    def classify_function(self, fn):
        """What a function of the wrapper's setup assignment is, from its shape."""
        params = fn["args"]
        body = fn["body"]["body"]
        found = {"while": False, "for": False, "inner": None}

        def look(x):
            if x["type"] in ("AstStatWhile", "AstStatRepeat"):
                found["while"] = True
            if x["type"] == "AstStatFor":
                found["for"] = True
        walk(fn["body"], look)
        if len(params) in (4, 5) and found["while"]:
            n_ifs = [0]
            walk(fn["body"], lambda x: n_ifs.__setitem__(0, n_ifs[0] + 1) if x["type"] == "AstStatIf" else None)
            if n_ifs[0] > 20:
                return "dispatcher"
        if len(params) in (2, 3):
            # maker: local q = h(t); local N = function(<params>) return F(o, {<params>}, t, q) end; return N
            inner = [x for x in functions_in(fn["body"])]
            if len(inner) == 1:
                f = inner[0]
                return Maker(None if f.get("vararg") else len(f["args"]))
        if len(params) == 0 and 3 <= len(body) <= 4 and body[-1]["type"] == "AstStatReturn":
            return "alloc"
        if len(params) == 1:
            if found["while"]:
                return "release_list"
            if found["for"]:
                return "retain"
            return "release"
        return None

    # ---- the dispatcher
    PARAM_ORDER = ("state", "args", "cells", "gc", "mask")

    def dispatcher_params(self):
        """{role: parameter position} of the dispatcher `F`. The older build always has
        `F(state, args, cells, gc)`; the newer one shuffles them, so they are read off the
        maker's `return F(...)` call: the maker's own parameters are (entry, cells, mask), `{...}`
        is the argument array and a local the maker made is the gc handle."""
        fn = self.dispatcher_fn
        if self.dispatcher_loop_kind() != "repeat":
            return {r: i for i, r in enumerate(self.PARAM_ORDER[:len(fn["args"])])}
        mk = getattr(self, "maker_fn", None)
        if mk is None:
            raise ModelError("no closure maker")
        mp = {a["location"]: i for i, a in enumerate(mk["args"])}
        calls = []
        walk(mk["body"], lambda x: calls.append(x) if x["type"] == "AstExprCall"
             and x["func"]["type"] == "AstExprLocal" and x["func"]["local"]["location"] == self.disp_loc else None)
        if len(calls) != 1 or len(calls[0]["args"]) != len(fn["args"]):
            raise ModelError("the maker does not call the dispatcher in the expected way")
        out = {}
        for i, a in enumerate(calls[0]["args"]):
            if a["type"] == "AstExprTable" and len(a["items"]) == 1 and a["items"][0]["value"]["type"] == "AstExprVarargs":
                out["args"] = i
            elif a["type"] == "AstExprLocal":
                k = mp.get(a["local"]["location"])
                out[{0: "state", 1: "cells", 2: "mask", None: "gc"}[k]] = i
            else:
                raise ModelError("unexpected maker call argument")
        if set(out) != {"state", "args", "cells", "gc", "mask"}:
            raise ModelError("dispatcher parameters not recognised: %s" % sorted(out))
        return out

    def find_dispatcher(self):
        fn = self.dispatcher_fn
        self.dispatcher = fn
        self.params = [a["location"] for a in fn["args"]]
        self.param_pos = self.dispatcher_params()
        regs = {}
        names = []
        # registers 0..n: state, args, cells, gc[, mask] (lift.py relies on this), then the locals
        for role in self.PARAM_ORDER:
            if role in self.param_pos:
                a = fn["args"][self.param_pos[role]]
                regs[a["location"]] = len(regs)
                names.append(a["name"])
        self.mask_reg = regs[fn["args"][self.param_pos["mask"]]["location"]] if "mask" in self.param_pos else None
        loop = None
        for s in fn["body"]["body"]:
            if s["type"] == "AstStatLocal":
                for v in s["vars"]:
                    regs[v["location"]] = len(regs)
                    names.append(v["name"])
            elif s["type"] in ("AstStatWhile", "AstStatRepeat"):
                loop = s
        self.regs, self.reg_names, self.loop = regs, names, loop
        # after the loop: `return unpack(<return array>)`
        self.ret_reg = None
        rets = []
        walk(fn["body"], lambda x: rets.append(x) if x["type"] == "AstStatReturn" else None)
        for s in rets:
            if len(s["list"]) == 1 and s["list"][0]["type"] == "AstExprCall":
                a = s["list"][0]["args"]
                if len(a) >= 1 and a[0]["type"] == "AstExprLocal" and \
                        all(const_eval(x) == 1 for x in a[1:]):
                    self.ret_reg = regs.get(a[0]["local"]["location"])
        if self.ret_reg is None:
            raise ModelError("dispatcher does not return an array register")
        self.state_loc = fn["args"][self.param_pos["state"]]["location"]
        # prologue locals with a table value: the constant table (`local k = {[n] = number, ...}`)
        # and plain `{}` tables (fresh per call)
        self.consts = {}
        self.const_regs = []
        self.table_regs = []
        self.consts_loc = None
        for s in fn["body"]["body"]:
            if s["type"] == "AstStatLocal" and len(s["values"]) == 1 and s["values"][0]["type"] == "AstExprTable":
                items = s["values"][0]["items"]
                if len(items) > 8 and all(i["kind"] == "general" and const_eval(i["key"]) is not None
                                          and const_eval(i["value"]) is not None for i in items):
                    t = {int(const_eval(i["key"])): const_eval(i["value"]) for i in items}
                    self.consts = t
                    self.consts_loc = s["vars"][0]["location"]
                    self.const_regs.append(regs[self.consts_loc])
                elif not items:
                    self.table_regs.append(regs[s["vars"][0]["location"]])
        # the first plain table is the dispatcher's spill table: variables beyond the local limit
        # live in `S2[<number>]`, i.e. it is more registers (see LeafRun.index)
        self.spill_reg = self.table_regs.pop(0) if self.table_regs else None
        self.spill_keys = set()
        if self.spill_reg is not None:
            spill_loc = [loc for loc, i in regs.items() if i == self.spill_reg][0]

            def spill_use(x):
                if x["type"] == "AstExprIndexExpr" and x["expr"]["type"] == "AstExprLocal" \
                        and x["expr"]["local"]["location"] == spill_loc:
                    k = x["index"]
                    if k["type"] == "AstExprConstantNumber":
                        self.spill_keys.add(int(k["value"]))
            walk(fn["body"], spill_use)

    def build_leaves(self):
        leaves = []

        def descend(block, lo, hi):
            stmts = [block] if block["type"] == "AstStatIf" else block["body"]
            if len(stmts) == 1 and stmts[0]["type"] == "AstStatIf" and self.is_dispatch_if(stmts[0]):
                n = stmts[0]
                c = n["condition"]
                l, r = c["left"], c["right"]
                dl = self.state_offset(l)
                if dl is None:
                    # N > state + d   ==   state < N - d
                    N = int(const_eval(l)) - self.state_offset(r)
                    tlo, thi, elo, ehi = lo, min(hi, N), max(lo, N), hi
                else:
                    N = int(const_eval(r)) - dl
                    if c["op"] == "CompareLt":              # state + d < N'
                        tlo, thi, elo, ehi = lo, min(hi, N), max(lo, N), hi
                    else:                                    # state + d > N'
                        tlo, thi, elo, ehi = max(lo, N + 1), hi, lo, min(hi, N + 1)
                descend(n["thenbody"], tlo, thi)
                if n.get("elsebody"):
                    descend(n["elsebody"], elo, ehi)
                return
            leaves.append(Leaf(0, lo, hi, stmts))
        descend(self.loop["body"], -INF, INF)
        leaves.sort(key=lambda l: l.lo)
        for i, l in enumerate(leaves):
            l.idx = i
        self.leaves = leaves
        self.los = [l.lo for l in leaves]
        for l in leaves:
            l.reads_state = self.reads_state_first(l.stmts)

    def reads_state_first(self, stmts):
        """Does the leaf read the incoming state before assigning it?"""
        loc = self.state_loc
        found = []

        def reads(n):
            hit = []
            walk(n, lambda x: hit.append(1) if x["type"] == "AstExprLocal" and x["local"]["location"] == loc else None)
            return bool(hit)
        for s in stmts:
            if s["type"] != "AstStatAssign":
                return True
            if any(reads(v) for v in s["values"]):
                return True
            for v in s["vars"]:
                if v["type"] == "AstExprLocal":
                    if v["local"]["location"] == loc:
                        return False
                elif reads(v):
                    return True
        return False

    def state_offset(self, n):
        """d if the expression is `state + d` (also `d + state`, `state - d`, plain `state`:
        d = 0) with d a constant; None for anything else."""
        t = n["type"]
        if t == "AstExprGroup":
            return self.state_offset(n["expr"])
        if t == "AstExprLocal":
            return 0 if n["local"]["location"] == self.state_loc else None
        if t == "AstExprBinary" and n["op"] in ("Add", "Sub"):
            d = self.state_offset(n["left"])
            if d is not None:
                c = const_eval(n["right"])
                if c is not None:
                    return d + c if n["op"] == "Add" else d - c
            elif n["op"] == "Add":
                d = self.state_offset(n["right"])
                c = const_eval(n["left"])
                if d is not None and c is not None:
                    return d + c
        return None

    def is_dispatch_if(self, n):
        c = n["condition"]
        if c["type"] != "AstExprBinary" or c["op"] not in ("CompareLt", "CompareGt"):
            return False
        l, r = c["left"], c["right"]
        if const_eval(l) is not None and self.state_offset(r) is not None:
            return c["op"] == "CompareGt"
        if const_eval(r) is not None and self.state_offset(l) is not None:
            return True
        return False

    def leaf_for(self, state):
        import bisect
        return self.leaves[bisect.bisect_right(self.los, state) - 1]


def _num(v):
    return int(v) if float(v).is_integer() and abs(v) < 2 ** 53 else v


def const_eval(node):
    """A number if `node` is a constant expression (a literal, or +, -, *, // or % of constant
    expressions): some builds write every numeric literal this way to dodge a plain grep/scan.
    None when it reads anything that is not a constant."""
    t = node["type"]
    if t == "AstExprConstantNumber":
        return _num(node["value"])
    if t == "AstExprGroup":
        return const_eval(node["expr"])
    if t == "AstExprUnary" and node["op"] == "Minus":
        v = const_eval(node["expr"])
        return None if v is None else -v
    if t == "AstExprBinary" and node["op"] in ("Add", "Sub", "Mul", "FloorDiv", "Mod"):
        a, b = const_eval(node["left"]), const_eval(node["right"])
        if a is None or b is None:
            return None
        if node["op"] == "Add":
            return _num(a + b)
        if node["op"] == "Sub":
            return _num(a - b)
        if node["op"] == "Mul":
            return _num(a * b)
        if b == 0:
            return None
        return _num(a // b) if node["op"] == "FloorDiv" else _num(a % b)
    return None


def load(path):
    with open(path, "rb") as f:
        source = f.read().decode("latin-1")
    return Model(parse_ast(path), source)
