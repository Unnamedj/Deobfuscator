"""
Dead set-up code. Once the string constants are decrypted, the decryptor the script builds at the top
of its main function (an assembler, a keystream, a proxy table) is never called, but it stays in the
lifted code. This pass removes the statements at the start of a function body that only compute
values nothing after them reads: a statement is dead when it touches only locals declared in that
prefix and library functions that have no outside effect.
"""
from obfuscators.zeus import strings as S
from obfuscators.zeus.looptext import Walk, line_start

PURE_GLOBALS = {"string", "math", "table", "utf8", "bit32", "setmetatable", "getmetatable", "select", "type",
                "tostring", "tonumber", "rawget", "rawset", "rawequal", "rawlen", "next", "pairs", "ipairs",
                "unpack", "typeof"}
PURE_LIB = {("table", "remove"), ("table", "insert"), ("table", "concat"), ("table", "unpack"),
            ("table", "pack"), ("table", "move"), ("table", "sort")}
EFFECT_FREE_LIBS = {"string", "math", "utf8", "bit32"}


def sub(n):
    for v in n.values():
        if isinstance(v, (dict, list)):
            yield v


def nodes(n):
    stack = [n]
    while stack:
        x = stack.pop()
        if isinstance(x, list):
            stack.extend(x)
        elif isinstance(x, dict):
            yield x
            stack.extend(sub(x))


class Prefix:
    def __init__(self, w, src):
        self.w, self.src = w, src

    def pos(self, loc):
        import re
        m = re.match(r"(\d+),(\d+)", loc)
        return self.src.starts[int(m.group(1))] + int(m.group(2))

    def pure(self, stmt, lo):
        """Does the statement affect nothing but locals declared at or after byte `lo`?"""
        if stmt["type"] in ("AstStatReturn", "AstStatBreak", "AstStatContinue"):
            return False
        for x in nodes(stmt):
            t = x.get("type")
            if t == "AstExprCall":
                f = x["func"]
                if x.get("self"):
                    return False
                if f["type"] == "AstExprLocal":
                    if self.pos(f["local"]["location"]) < lo:
                        return False        # a function from outside: unknown effects
                elif f["type"] == "AstExprGlobal":
                    if f["global"] not in PURE_GLOBALS:
                        return False
                elif f["type"] == "AstExprIndexName" and f["expr"]["type"] == "AstExprGlobal":
                    lib, name = f["expr"]["global"], f["index"]
                    if not (lib in EFFECT_FREE_LIBS or (lib, name) in PURE_LIB):
                        return False
                else:
                    return False
            elif t == "AstStatAssign":
                for v in x["vars"]:
                    base = v
                    while base["type"] in ("AstExprIndexExpr", "AstExprIndexName"):
                        base = base["expr"]
                    if base["type"] != "AstExprLocal" or self.pos(base["local"]["location"]) < lo:
                        return False
            elif t == "AstStatCompoundAssign":
                base = x["var"]
                while base["type"] in ("AstExprIndexExpr", "AstExprIndexName"):
                    base = base["expr"]
                if base["type"] != "AstExprLocal" or self.pos(base["local"]["location"]) < lo:
                    return False
            elif t == "AstExprGlobal":
                if x["global"] not in PURE_GLOBALS:
                    return False
        return True


def stmt_writes(p, stmt, fn_writes):
    out = set()
    for x in nodes(stmt):
        t = x.get("type")
        if t == "AstStatLocal":
            out.update(v["location"] for v in x["vars"])
        elif t == "AstStatLocalFunction":
            out.add(x["name"]["location"])
        elif t == "AstStatAssign":
            for v in x["vars"]:
                base = v
                while base["type"] in ("AstExprIndexExpr", "AstExprIndexName"):
                    base = base["expr"]
                if base["type"] == "AstExprLocal":
                    out.add(base["local"]["location"])
        elif t == "AstStatCompoundAssign":
            base = x["var"]
            while base["type"] in ("AstExprIndexExpr", "AstExprIndexName"):
                base = base["expr"]
            if base["type"] == "AstExprLocal":
                out.add(base["local"]["location"])
        elif t == "AstExprCall":
            # a call may change what it is handed, and what the called local function writes
            for a in x["args"]:
                if a["type"] == "AstExprLocal":
                    out.add(a["local"]["location"])
            f = x["func"]
            if f["type"] == "AstExprLocal":
                out |= fn_writes.get(f["local"]["location"], set())
    return out


def stmt_reads(stmt):
    return {x["local"]["location"] for x in nodes(stmt) if x.get("type") == "AstExprLocal"}


def function_writes(p, root):
    """For each local function / `local f = function`: the locals its body assigns (through aliases)."""
    out = {}
    for x in nodes(root):
        t = x.get("type")
        fn = loc = None
        if t == "AstStatLocalFunction":
            fn, loc = x["func"], x["name"]["location"]
        elif t == "AstStatLocal" and len(x["vars"]) == 1 and len(x["values"]) == 1 \
                and x["values"][0]["type"] == "AstExprFunction":
            fn, loc = x["values"][0], x["vars"][0]["location"]
        if fn is None:
            continue
        w = set()
        for y in nodes(fn["body"]):
            ty = y.get("type")
            if ty in ("AstStatAssign",):
                for v in y["vars"]:
                    base = v
                    while base["type"] in ("AstExprIndexExpr", "AstExprIndexName"):
                        base = base["expr"]
                    if base["type"] == "AstExprLocal":
                        w.add(base["local"]["location"])
            elif ty == "AstStatCompoundAssign":
                base = y["var"]
                while base["type"] in ("AstExprIndexExpr", "AstExprIndexName"):
                    base = base["expr"]
                if base["type"] == "AstExprLocal":
                    w.add(base["local"]["location"])
        out[loc] = w
    return out


def clean_body(p, body, fn_writes, edits):
    if not body:
        return
    lo = p.w.span(body[0])[0]
    m = 0
    while m < len(body) and p.pure(body[m], lo):
        m += 1
    if m < 3:
        return
    prefix = body[:m]
    end = p.w.span(prefix[-1])[1]
    declared = set()
    for st in prefix:
        if st["type"] == "AstStatLocal":
            declared.update(v["location"] for v in st["vars"])
        elif st["type"] == "AstStatLocalFunction":
            declared.add(st["name"]["location"])
    # locals of the prefix the rest of the program reads
    needed = {loc for loc in declared if any(a >= end for a, _ in p.w.refs[loc])}
    writes = [stmt_writes(p, st, fn_writes) & (declared | needed) for st in prefix]
    reads = [stmt_reads(st) & declared for st in prefix]
    keep = [False] * m
    changed = True
    while changed:
        changed = False
        for i in range(m):
            if not keep[i] and writes[i] & needed:
                keep[i] = True
                if not reads[i] <= needed:
                    needed |= reads[i]
                changed = True
    for i, st in enumerate(prefix):
        if not keep[i]:
            a, b = p.w.span(st)
            edits.append((line_start(p.src, a), b + 1 if p.src.data[b:b + 1] == b"\n" else b))


def clean(text):
    """(text, number of statements removed)"""
    data = text.encode("utf-8")
    src = S.Source(data)
    root = S.parse(data)
    w = Walk(root, src)
    p = Prefix(w, src)
    fnw = function_writes(p, root)
    edits = []
    clean_body(p, root["body"], fnw, edits)
    for x in nodes(root):
        if x.get("type") == "AstExprFunction":
            clean_body(p, x["body"]["body"], fnw, edits)
    # nested function bodies inside a removed statement would be listed twice
    edits.sort()
    kept, end = [], -1
    for a, b in edits:
        if a >= end:
            kept.append((a, b))
            end = b
    out = data
    for a, b in sorted(kept, reverse=True):
        out = out[:a] + out[b:]
    return out.decode("utf-8"), len(kept)
