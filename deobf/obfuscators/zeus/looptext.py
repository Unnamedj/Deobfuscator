"""
ZEUS compiles `for` loops into `while true do` loops over plain locals. This pass writes them back
as `for` statements on the lifted text (before the naming pass, which names `for` variables):

  generic   local f, s, c = pairs(t)                   for c, v in pairs(t) do
            while true do                       ->         <body>
                local v                                 end
                c, v = f(s, c)
                if c then <body> else break end
            end
  (the three values may arrive as `local T = table.pack(<call>)` plus `local x = T[i]`)

  numeric   local n = 0                                for n = 1, limit do
            while true do                       ->         <body>
                n += 1                                  end
                if n <= limit then <body> else break end
            end

A loop is rewritten only when every use of the loop's locals is accounted for (a half rewrite
would change what the code does).
"""
import re
from collections import defaultdict

from obfuscators.zeus import strings as S


class Walk:
    """One pass over the AST: references to every local, assignments, parents."""

    def __init__(self, root, src):
        self.src = src
        self.refs = defaultdict(list)       # declaration location -> [(start, end)]
        self.assigns = defaultdict(list)    # declaration location -> [statement nodes]
        self.decl = {}                      # declaration location -> (statement, index in its block, block list)
        self.parent = {}
        self.fn_of_ref = {}
        stack = [(root, None, None)]
        while stack:
            n, par, blk = stack.pop()
            if isinstance(n, list):
                for x in n:
                    stack.append((x, par, n))
                continue
            if not isinstance(n, dict):
                continue
            if "type" in n:
                self.parent[id(n)] = par
                t = n["type"]
                if t == "AstExprLocal":
                    self.refs[n["local"]["location"]].append(self.span(n))
                elif t == "AstStatAssign":
                    for v in n["vars"]:
                        if v["type"] == "AstExprLocal":
                            self.assigns[v["local"]["location"]].append(n)
                elif t == "AstStatCompoundAssign":
                    if n["var"]["type"] == "AstExprLocal":
                        self.assigns[n["var"]["local"]["location"]].append(n)
                elif t == "AstStatLocal" and blk is not None:
                    for v in n["vars"]:
                        self.decl[v["location"]] = (n, blk.index(n) if n in blk else -1, blk)
                par = n
            for k, v in n.items():
                if isinstance(v, (dict, list)):
                    stack.append((v, par, v if isinstance(v, list) else blk))

    def span(self, n):
        l1, c1, l2, c2 = S.span(n)
        return self.src.starts[l1] + c1, self.src.starts[l2] + c2

    def refs_in(self, loc, lo, hi):
        return sum(1 for a, b in self.refs[loc] if lo <= a and b <= hi)


def loc_of(n):
    return n["local"]["location"] if n["type"] == "AstExprLocal" else None


def is_true(n):
    return n["type"] == "AstExprConstantBool" and n["value"] is True


def only_break(blockish):
    body = blockish["body"] if blockish["type"] == "AstStatBlock" else [blockish]
    return len(body) == 1 and body[0]["type"] == "AstStatBreak"


def dedent(text, tabs=1):
    """Remove `tabs` leading tabs of every line of a statement list (bytes)."""
    pre = b"\t" * tabs
    return b"\n".join(l[len(pre):] if l.startswith(pre) else l for l in text.split(b"\n"))


def block_text(w, stmts, src, dent):
    """Source of a statement list, one indentation level shallower."""
    if not stmts:
        return b""
    a = w.span(stmts[0])[0]
    b = w.span(stmts[-1])[1]
    # start at the beginning of the first statement's line to keep its indentation
    ls = src.data.rfind(b"\n", 0, a) + 1
    return dedent(src.data[ls:b], dent)


def line_start(src, pos):
    return src.data.rfind(b"\n", 0, pos) + 1


def try_generic(w, loop, src):
    body = loop["body"]["body"]
    if not is_true(loop["condition"]):
        return None
    stmts = list(body)
    declared = []
    while stmts and stmts[0]["type"] == "AstStatLocal" and not stmts[0]["values"]:
        declared += [v for v in stmts[0]["vars"]]
        stmts = stmts[1:]
    if len(stmts) != 2:
        return None
    asg, iff = stmts
    if asg["type"] != "AstStatAssign" or iff["type"] != "AstStatIf" or len(asg["values"]) != 1:
        return None
    call = asg["values"][0]
    if call["type"] != "AstExprCall" or call.get("self") or len(call["args"]) != 2:
        return None
    f, s, c = loc_of(call["func"]), loc_of(call["args"][0]), loc_of(call["args"][1])
    targets = [loc_of(v) for v in asg["vars"]]
    if None in (f, s, c) or None in targets or targets[0] != c:
        return None
    if iff["condition"]["type"] != "AstExprLocal" or loc_of(iff["condition"]) != c:
        return None
    if not iff.get("elsebody") or not only_break(iff["elsebody"]):
        return None
    lo, hi = w.span(loop)
    # where the three values come from: the statements right before the loop
    info = w.decl.get(c)
    blk = None
    for loc in (f, s, c):
        d = w.decl.get(loc)
        if d is None:
            return None
        blk = d[2]
    idx = blk.index(loop) if loop in blk else -1
    if idx < 0:
        return None
    defs = []           # statements to remove
    explist = None
    j = idx - 1
    # P1: local f, s, c = <exprs>
    st = blk[j] if j >= 0 else None
    if st is not None and st["type"] == "AstStatLocal" and [v["location"] for v in st["vars"]] == [f, s, c]:
        explist = b", ".join(src.text(v) for v in st["values"])
        defs = [st]
    elif st is not None:
        # P2: three `local x = T[i]` moves in front of `local T = table.pack(<call>)`
        picks = {}
        k = j
        while k >= 0 and len(picks) < 3:
            q = blk[k]
            if q["type"] == "AstStatLocal" and len(q["vars"]) == 1 and len(q["values"]) == 1 \
                    and q["values"][0]["type"] == "AstExprIndexExpr" \
                    and q["values"][0]["index"]["type"] == "AstExprConstantNumber" \
                    and q["vars"][0]["location"] in (f, s, c):
                picks[int(q["values"][0]["index"]["value"])] = (q, loc_of(q["values"][0]["expr"]))
                k -= 1
            else:
                break
        if sorted(picks) != [1, 2, 3] or len({p[1] for p in picks.values()}) != 1:
            return None
        if {picks[1][0]["vars"][0]["location"], picks[2][0]["vars"][0]["location"],
                picks[3][0]["vars"][0]["location"]} != {f, s, c}:
            return None
        if picks[1][0]["vars"][0]["location"] != f or picks[2][0]["vars"][0]["location"] != s \
                or picks[3][0]["vars"][0]["location"] != c:
            return None
        pk = blk[k] if k >= 0 else None
        T = picks[1][1]
        if pk is None or pk["type"] != "AstStatLocal" or pk["vars"][0]["location"] != T or len(pk["values"]) != 1:
            return None
        v = pk["values"][0]
        if v["type"] != "AstExprCall" or src.text(v["func"]) != b"table.pack":
            return None
        explist = b", ".join(src.text(a) for a in v["args"])
        defs = [pk] + [p[0] for p in picks.values()]
        if w.refs_in(T, 0, 1 << 60) != 3:
            return None
    else:
        return None
    # every use of the iterator state is accounted for
    body_lo, body_hi = w.span(iff["thenbody"])
    for loc in (f, s):
        if len(w.refs[loc]) != 1 or not (lo <= w.refs[loc][0][0] < hi):
            return None
    for a, b in w.refs[c]:
        if not (lo <= a < hi):
            return None         # read after the loop
    for node in w.assigns[c]:
        if node is not asg:
            return None
    names = [src.text(v) for v in asg["vars"]]
    lvars = b", ".join(src.text(v) for v in asg["vars"])
    for loc in targets[1:]:
        for a, b in w.refs[loc]:
            if not (lo <= a < hi):
                return None
    body_text = block_text(w, iff["thenbody"]["body"], src, 1)
    ind = src.data[line_start(src, lo):lo]
    if ind.strip(b"\t"):
        ind = b"\t" * (len(ind) - len(ind.lstrip(b"\t")))
    new = b"for %s in %s do\n%s\n%send" % (lvars, explist, body_text, ind) if body_text else \
        b"for %s in %s do\n%send" % (lvars, explist, ind)
    start = min(w.span(d)[0] for d in defs)
    # replace [first def .. loop end]: the defs sit right before the loop
    return (start, hi, new)


def try_numeric(w, loop, src):
    if not is_true(loop["condition"]):
        return None
    body = loop["body"]["body"]
    if len(body) != 2:
        return None
    inc, iff = body
    n = step = None
    if inc["type"] == "AstStatCompoundAssign" and inc["op"] == "Add" and inc["var"]["type"] == "AstExprLocal" \
            and inc["value"]["type"] == "AstExprConstantNumber":
        n, step = loc_of(inc["var"]), inc["value"]["value"]
    elif inc["type"] == "AstStatAssign" and len(inc["vars"]) == 1 and len(inc["values"]) == 1 \
            and inc["vars"][0]["type"] == "AstExprLocal" and inc["values"][0]["type"] == "AstExprBinary" \
            and inc["values"][0]["op"] == "Add":
        v = inc["values"][0]
        a, b = v["left"], v["right"]
        me = loc_of(inc["vars"][0])
        if loc_of(b) == me and a["type"] == "AstExprConstantNumber":
            n, step = me, a["value"]
        elif loc_of(a) == me and b["type"] == "AstExprConstantNumber":
            n, step = me, b["value"]
    if n is None or step == 0 or step != int(step):
        return None
    step = int(step)
    if iff["type"] != "AstStatIf" or not iff.get("elsebody") or not only_break(iff["elsebody"]):
        return None
    c = iff["condition"]
    if c["type"] != "AstExprBinary":
        return None
    # `n <= limit` / `limit >= n` (step > 0), `n >= limit` / `limit <= n` (step < 0)
    op, l, r = c["op"], c["left"], c["right"]
    if loc_of(l) == n:
        lim, o = r, op
    elif loc_of(r) == n:
        lim, o = l, {"CompareLe": "CompareGe", "CompareGe": "CompareLe", "CompareLt": "CompareGt",
                     "CompareGt": "CompareLt"}.get(op)
    else:
        return None
    ok = {"CompareLe": step > 0, "CompareGe": step < 0}.get(o)
    if not ok:
        return None
    lo, hi = w.span(loop)
    # the limit is evaluated once by a `for`: a constant or a local nothing in the loop assigns
    if lim["type"] == "AstExprLocal":
        ll = loc_of(lim)
        if any(lo <= w.span(a)[0] < hi for a in w.assigns[ll]):
            return None
    elif lim["type"] != "AstExprConstantNumber":
        return None
    # the counter: declared with a number, assigned only by the increment, used only in the loop
    d = w.decl.get(n)
    if d is None or len(d[0]["vars"]) != 1 or len(d[0]["values"]) != 1 \
            or d[0]["values"][0]["type"] != "AstExprConstantNumber":
        return None
    if len(w.assigns[n]) != 1 or w.assigns[n][0] is not inc:
        return None
    for a, b in w.refs[n]:
        if not (lo <= a < hi):
            return None
    # (a closure naming the counter would see one variable instead of one per iteration)
    for a, b in w.refs[n]:
        if hits_function(w, iff["thenbody"], a):
            return None
    start = d[0]["values"][0]["value"] + step
    if start != int(start):
        return None
    name = src.text(d[0]["vars"][0]) if False else d[0]["vars"][0]["name"].encode("latin-1")
    limit = src.text(lim)
    head = b"for %s = %d, %s%s do" % (name, int(start), limit, b"" if step == 1 else b", %d" % step)
    body_text = block_text(w, iff["thenbody"]["body"], src, 1)
    ind = src.data[line_start(src, lo):lo]
    ind = b"\t" * (len(ind) - len(ind.lstrip(b"\t")))
    new = head + b"\n" + (body_text + b"\n" if body_text else b"") + ind + b"end"
    # the declaration statement goes too (it must sit in the loop's own block)
    return (lo, hi, new, d[0])


def hits_function(w, block, pos):
    """Is source position pos inside a function expression of block?"""
    stack = [block]
    while stack:
        x = stack.pop()
        if isinstance(x, list):
            stack.extend(x)
        elif isinstance(x, dict):
            if x.get("type") == "AstExprFunction":
                a, b = w.span(x)
                if a <= pos < b:
                    return True
                continue
            stack.extend(v for v in x.values() if isinstance(v, (dict, list)))
    return False


def rewrite_once(text):
    data = text.encode("utf-8")
    src = S.Source(data)
    root = S.parse(data)
    w = Walk(root, src)
    loops = []
    stack = [root]
    while stack:
        x = stack.pop()
        if isinstance(x, list):
            stack.extend(x)
        elif isinstance(x, dict):
            if x.get("type") == "AstStatWhile":
                loops.append(x)
            stack.extend(v for v in x.values() if isinstance(v, (dict, list)))
    edits = []
    for lp in loops:
        e = try_generic(w, lp, src) or try_numeric(w, lp, src)
        if e:
            edits.append(e)
    if not edits:
        return text, 0
    # drop the declaration of a numeric counter along with its loop
    final = []
    for e in edits:
        final.append((e[0], e[1], e[2]))
        if len(e) > 3:
            a, b = w.span(e[3])
            ls = line_start(src, a)
            final.append((ls, b + 1 if data[b:b + 1] == b"\n" else b, b""))
    # nested rewrites would overlap: keep the outer-most ones this round
    final.sort(key=lambda e: (e[0], -e[1]))
    kept, end = [], -1
    for e in final:
        if e[0] >= end:
            kept.append(e)
            end = max(end, e[1])
    out = data
    for a, b, new in sorted(kept, reverse=True):
        out = out[:a] + new + out[b:]
    return out.decode("utf-8"), len(kept)


def rewrite(text, rounds=6):
    total = 0
    for _ in range(rounds):
        text, n = rewrite_once(text)
        total += n
        if not n:
            break
    return text, total
