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
        self.find_wrapper()
        self.find_roles()
        self.find_dispatcher()
        self.build_leaves()

    # ---- wrapper: `return (function(<params>) <setup> end)(newproxy, select, env, ...)`
    def find_wrapper(self):
        ret = None
        for s in self.root["body"]:
            if s["type"] == "AstStatReturn" and len(s["list"]) == 1 and s["list"][0]["type"] == "AstExprCall":
                ret = s["list"][0]
        if ret is None:
            raise ModelError("no `return (function(...) ... end)(...)` wrapper")
        fn = ret["func"]
        while fn["type"] == "AstExprGroup":
            fn = fn["expr"]
        if fn["type"] != "AstExprFunction":
            raise ModelError("wrapper is not a function")
        self.wrapper, self.wrapper_call = fn, ret

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
        for v, val in zip(setup["vars"], setup["values"]):
            loc = v["local"]["location"]
            if val["type"] == "AstExprFunction":
                kind = self.classify_function(val)
                if kind == "dispatcher":
                    disp_loc = loc
                    self.dispatcher_fn = val
                    roles[loc] = Special("dispatcher")
                elif isinstance(kind, Maker):
                    roles[loc] = kind
                elif kind:
                    roles[loc] = Special(kind)
            elif val["type"] == "AstExprTable" and not val["items"]:
                roles.setdefault(loc, Special("table"))
        if disp_loc is None:
            raise ModelError("no dispatcher function in the wrapper")
        self.name_roles(setup, roles)
        # the cell store `w` is the table the alloc function's sibling reads: the one cells are read from
        self.disp_loc = disp_loc
        self.roles = roles
        # entry: `return (maker(<state>, {}))(unpack(<script args>))`
        callee = self.wrapper_call["func"]
        # (the wrapper call's own callee is the function; the entry is in the wrapper's return)
        ret = [s for s in w["body"]["body"] if s["type"] == "AstStatReturn"]
        if not ret:
            raise ModelError("no entry call in the wrapper")
        call = ret[-1]["list"][0]
        f = call["func"]
        while f["type"] == "AstExprGroup":
            f = f["expr"]
        if f["type"] != "AstExprCall" or not f["args"] or f["args"][0]["type"] != "AstExprConstantNumber":
            raise ModelError("unexpected entry call %s" % self.text(call))
        self.entry = int(f["args"][0]["value"])
        self.entry_maker = roles.get(f["func"]["local"]["location"]) if f["func"]["type"] == "AstExprLocal" else None

    def name_roles(self, setup, roles):
        """Of the wrapper's two empty tables, the refcount table is the one the alloc function
        writes (`k[n] = 1`); the other holds the cells' values."""
        tables = [loc for loc, r in roles.items() if isinstance(r, Special) and r.kind == "table"]
        refc = None
        for v, val in zip(setup["vars"], setup["values"]):
            if roles.get(v["local"]["location"]) == "alloc" or (
                    isinstance(roles.get(v["local"]["location"]), Special)
                    and roles[v["local"]["location"]].kind == "alloc"):
                for s in val["body"]["body"]:
                    if s["type"] == "AstStatAssign" and s["vars"][0]["type"] == "AstExprIndexExpr":
                        e = s["vars"][0]["expr"]
                        if e["type"] == "AstExprLocal":
                            refc = e["local"]["location"]
        for loc in tables:
            roles[loc] = Special("refcount" if loc == refc else "cells")

    def classify_function(self, fn):
        """What a function of the wrapper's setup assignment is, from its shape."""
        params = fn["args"]
        body = fn["body"]["body"]
        found = {"while": False, "for": False, "inner": None}

        def look(x):
            if x["type"] == "AstStatWhile":
                found["while"] = True
            if x["type"] == "AstStatFor":
                found["for"] = True
        walk(fn["body"], look)
        if len(params) == 4 and found["while"]:
            n_ifs = [0]
            walk(fn["body"], lambda x: n_ifs.__setitem__(0, n_ifs[0] + 1) if x["type"] == "AstStatIf" else None)
            if n_ifs[0] > 20:
                return "dispatcher"
        if len(params) == 2:
            # maker: local q = h(t); local N = function(<params>) return F(o, {<params>}, t, q) end; return N
            inner = [x for x in functions_in(fn["body"])]
            if len(inner) == 1:
                f = inner[0]
                return Maker(None if f.get("vararg") else len(f["args"]))
        if len(params) == 0 and len(body) == 3 and body[-1]["type"] == "AstStatReturn":
            return "alloc"
        if len(params) == 1:
            if found["while"]:
                return "release_list"
            if found["for"]:
                return "retain"
            return "release"
        return None

    # ---- the dispatcher
    def find_dispatcher(self):
        fn = self.dispatcher_fn
        self.dispatcher = fn
        self.params = [a["location"] for a in fn["args"]]
        regs = {}
        names = []
        for a in fn["args"]:
            regs[a["location"]] = len(regs)
            names.append(a["name"])
        loop = None
        for s in fn["body"]["body"]:
            if s["type"] == "AstStatLocal":
                for v in s["vars"]:
                    regs[v["location"]] = len(regs)
                    names.append(v["name"])
            elif s["type"] == "AstStatWhile":
                loop = s
        if loop is None:
            raise ModelError("dispatcher without a loop")
        self.regs, self.reg_names, self.loop = regs, names, loop
        # after the loop: `return unpack(<return array>)`
        self.ret_reg = None
        for s in fn["body"]["body"]:
            if s["type"] == "AstStatReturn" and len(s["list"]) == 1 and s["list"][0]["type"] == "AstExprCall":
                a = s["list"][0]["args"]
                if len(a) == 1 and a[0]["type"] == "AstExprLocal":
                    self.ret_reg = regs.get(a[0]["local"]["location"])
        if self.ret_reg is None:
            raise ModelError("dispatcher does not return an array register")
        self.state_loc = fn["args"][0]["location"]
        # prologue locals with a table value: the constant table (`local k = {[n] = number, ...}`)
        # and plain `{}` tables (fresh per call)
        self.consts = {}
        self.const_regs = []
        self.table_regs = []
        self.consts_loc = None
        for s in fn["body"]["body"]:
            if s["type"] == "AstStatLocal" and len(s["values"]) == 1 and s["values"][0]["type"] == "AstExprTable":
                items = s["values"][0]["items"]
                if len(items) > 8 and all(i["kind"] == "general" and i["key"]["type"] == "AstExprConstantNumber"
                                          and i["value"]["type"] in ("AstExprConstantNumber", "AstExprUnary")
                                          for i in items):
                    t = {}
                    for i in items:
                        v = i["value"]
                        if v["type"] == "AstExprUnary":
                            v = {"value": -v["expr"]["value"]}
                        t[int(i["key"]["value"])] = _num(v["value"])
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
                if l["type"] == "AstExprConstantNumber":
                    N = int(l["value"])                     # N > state
                    tlo, thi, elo, ehi = lo, min(hi, N), max(lo, N), hi
                else:
                    N = int(r["value"])
                    if c["op"] == "CompareLt":              # state < N
                        tlo, thi, elo, ehi = lo, min(hi, N), max(lo, N), hi
                    else:                                    # state > N
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

    def is_dispatch_if(self, n):
        c = n["condition"]
        if c["type"] != "AstExprBinary" or c["op"] not in ("CompareLt", "CompareGt"):
            return False
        l, r = c["left"], c["right"]
        ls = self.state_loc
        if l["type"] == "AstExprConstantNumber" and r["type"] == "AstExprLocal":
            return r["local"]["location"] == ls and c["op"] == "CompareGt"
        if r["type"] == "AstExprConstantNumber" and l["type"] == "AstExprLocal":
            return l["local"]["location"] == ls
        return False

    def leaf_for(self, state):
        import bisect
        return self.leaves[bisect.bisect_right(self.los, state) - 1]


def _num(v):
    return int(v) if float(v).is_integer() and abs(v) < 2 ** 53 else v


def load(path):
    with open(path, "rb") as f:
        source = f.read().decode("latin-1")
    return Model(parse_ast(path), source)
