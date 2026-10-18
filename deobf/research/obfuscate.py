#!/usr/bin/env python3
"""
Test-sample generator: obfuscates a Luau script so we can test the deobfuscator against a
known original. We have no original+obfuscated pairs for ZEUS or HYDRA (only the obfuscated
side), so every semantic check against those has to be a behaviour trace, not a diff. This
produces pairs *with* a known source, for regression tests and for exercising each pipeline.

It is a semantics-preserving source rewriter, not a protection product: every transform is a
local, provably-equivalent edit, and `--check` runs the original and the obfuscated side under
luau and compares their output to prove the logic is unchanged.

Levels (cumulative; `--level N` applies 1..N):
  1  string + integer-literal encoding (decoded by a small prelude at run time)
  2  + local-variable renaming (scope-safe: every distinct declaration gets a unique name)
  3  + a loadstring packer layer around the whole thing (HYDRA-style, one layer)

Usage:
    python research/obfuscate.py IN.lua -o OUT.lua [--level 3] [--seed N]
    python research/obfuscate.py IN.lua --check         # obfuscate, run both, diff output
"""
import argparse
import json
import os
import random
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import harness  # noqa: E402

STR_KEY = 157


def line_starts(data):
    starts = [0]
    for m in re.finditer(b"\n", data):
        starts.append(m.end())
    return starts


def parse_ast(data):
    """luau-ast of latin-1 bytes `data` (a Luau source)."""
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".luau", prefix="obf_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        out = subprocess.run([harness.luau_ast(), path], capture_output=True)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    text = out.stdout.decode("latin-1")
    start = text.find('{"root"')
    if start < 0:
        raise ValueError("luau-ast could not parse the script:\n" + (out.stdout + out.stderr).decode("latin-1")[:400])
    return json.loads(text[start:])["root"]


def loc(n):
    m = re.match(r"(\d+),(\d+) - (\d+),(\d+)", n["location"])
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))


def abs_span(starts, n):
    l1, c1, l2, c2 = loc(n)
    return starts[l1] + c1, starts[l2] + c2


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


def lua_bytes_literal(b):
    """Decimal-escaped Lua string literal for arbitrary bytes (byte-exact, no newline folding).
    Escapes are always 3 digits: `\\9` + `2` would read back as byte 92, not 9 then '2'."""
    out = []
    for c in b:
        if 32 <= c < 127 and c not in (34, 92):
            out.append(chr(c))
        else:
            out.append("\\%03d" % c)
    return '"' + "".join(out) + '"'


# --------------------------------------------------------------------------
# level 1: string + integer literal encoding

def collect_strings(root, starts, data):
    """[(start, end, decoded bytes)] for every string-constant literal that is a real value.
    Record-style table keys (`{ name = v }`) are stored as AstExprConstantString in the AST but
    are field names, not values: `__S(i) = v` is a syntax error, so they are excluded."""
    record_keys = set()

    def mark(n):
        if n["type"] == "AstExprTable":
            for it in n["items"]:
                if it.get("kind") == "record" and isinstance(it.get("key"), dict):
                    record_keys.add(id(it["key"]))
    walk(root, mark)

    out = []

    def f(n):
        if n["type"] == "AstExprConstantString" and id(n) not in record_keys:
            a, b = abs_span(starts, n)
            out.append((a, b, n["value"].encode("latin-1")))
    walk(root, f)
    return out


def collect_numbers(root, starts):
    """[(start, end, int value)] for every integer-valued numeric literal in a safe range."""
    out = []

    def f(n):
        if n["type"] == "AstExprConstantNumber":
            v = n["value"]
            if isinstance(v, (int, float)) and float(v).is_integer() and abs(v) < 2 ** 52:
                a, b = abs_span(starts, n)
                out.append((a, b, int(v)))
    walk(root, f)
    return out


def enc_string(b):
    """XOR-ish per-position encoding; decoder: (byte - j - STR_KEY) % 256 (j 1-based)."""
    return bytes((b[p] + (p + 1) + STR_KEY) % 256 for p in range(len(b)))


def string_prelude(rng, blobs):
    """`__B`, `__S` definitions. blobs: list of already-encoded byte strings (1-based index)."""
    tbl = ",".join(lua_bytes_literal(x) for x in blobs)
    return (
        "local __B={%s}\n"
        "local function __S(i)local s=__B[i]local t={}for j=1,#s do "
        "t[j]=string.char((string.byte(s,j)-j-%d)%%256)end return table.concat(t)end\n"
    ) % (tbl, STR_KEY)


def enc_number(rng, v):
    """`(A - B)` with A - B == v; parenthesized so it is a primary expression anywhere."""
    r = rng.randint(1, 1 << 20)
    return "(%d - %d)" % (v + r, r)


# --------------------------------------------------------------------------
# level 2: local renaming

def collect_renames(root, starts, counter):
    """[(start, end, new name)] for every local declaration token and its references.
    Each distinct declaration (keyed by its source location) gets one fresh unique name, so
    references stay consistent and nothing can shadow or collide. Globals/fields are untouched.
    `self` and `...` are left alone (implicit / not real identifier tokens)."""
    decl_name = {}          # declaration location string -> new name

    def fresh():
        counter[0] += 1
        return "_%x" % counter[0]

    def decl(node):
        """Register an AstLocal declaration token; return its (span, newname) edit or None."""
        if node.get("name") in (None, "self", "..."):
            return None
        key = node["location"]
        nm = decl_name.get(key)
        if nm is None:
            nm = decl_name[key] = fresh()
        a, b = abs_span(starts, node)
        return (a, b, nm)

    edits = []

    def declare_all(nodes):
        for v in nodes:
            e = decl(v)
            if e:
                edits.append(e)

    def f(n):
        t = n["type"]
        if t == "AstStatLocal":
            declare_all(n["vars"])
        elif t == "AstStatLocalFunction":
            e = decl(n["name"])
            if e:
                edits.append(e)
        elif t == "AstExprFunction":
            declare_all(n["args"])
        elif t == "AstStatFor":
            e = decl(n["var"])
            if e:
                edits.append(e)
        elif t == "AstStatForIn":
            declare_all(n["vars"])
    walk(root, f)

    # references: every AstExprLocal points at its declaration via local.location
    def ref(n):
        if n["type"] == "AstExprLocal":
            key = n["local"]["location"]
            nm = decl_name.get(key)
            if nm is not None:
                a, b = abs_span(starts, n)
                edits.append((a, b, nm))
    walk(root, ref)
    return edits


# --------------------------------------------------------------------------
# level 3: loadstring packer

def pack(rng, inner_source):
    """Wrap `inner_source` (latin-1 str) in one loadstring layer: a key-stream XOR, decoded and
    loadstring'd at run time, then called with the forwarded varargs. Semantics identical."""
    data = inner_source.encode("latin-1")
    key = bytes(rng.randint(0, 255) for _ in range(16))
    enc = bytes(data[i] ^ key[i % len(key)] for i in range(len(data)))
    klit = ",".join(str(x) for x in key)
    # Luau has no infix bitwise operators: XOR is bit32.bxor, not `~`.
    return (
        "local __k={%s}\n"
        "local __e=%s\n"
        "local __o={}\n"
        "local __x=bit32.bxor\n"
        "for __i=1,#__e do __o[__i]=string.char(__x(string.byte(__e,__i),__k[(__i-1)%%16+1]))end\n"
        "return loadstring(table.concat(__o))(...)\n"
    ) % (klit, lua_bytes_literal(enc))


# --------------------------------------------------------------------------

def apply_edits(data, edits):
    """edits: list of (start, end, replacement str). Non-overlapping; applied right to left."""
    out = data
    for a, b, rep in sorted(edits, key=lambda e: e[0], reverse=True):
        out = out[:a] + rep.encode("latin-1") + out[b:]
    return out


def obfuscate(source, level=3, seed=0):
    """source: latin-1 str. Returns the obfuscated latin-1 str."""
    rng = random.Random(seed)
    data = source.encode("latin-1")
    root = parse_ast(data)
    starts = line_starts(data)

    edits = []
    blobs = []
    if level >= 1:
        for a, b, raw in collect_strings(root, starts, data):
            blobs.append(enc_string(raw))
            edits.append((a, b, "__S(%d)" % len(blobs)))
        for a, b, v in collect_numbers(root, starts):
            edits.append((a, b, enc_number(rng, v)))
    if level >= 2:
        edits += collect_renames(root, starts, [0])

    body = apply_edits(data, edits).decode("latin-1")
    if level >= 1:
        body = string_prelude(rng, blobs) + body
    if level >= 3:
        body = pack(rng, body)
    return body


# --------------------------------------------------------------------------
# verification: run both sides, compare output

CHECK_HARNESS = (
    "local __out={}\n"
    "local __p=print\n"
    "print=function(...)local t={}for i=1,select('#',...)do t[i]=tostring((select(i,...)))end "
    "__out[#__out+1]=table.concat(t,'\\t')end\n"
    "local __f=assert((loadstring or load)(%s,'=obfchk'))\n"
    "local __ok,__err=pcall(__f)\n"
    "print=__p\n"
    "__p('\\1OUT\\1'..(__ok and table.concat(__out,'\\n') or ('ERROR: '..tostring(__err))))\n"
)


def run_capture(source, luau, timeout=30):
    import tempfile
    prog = CHECK_HARNESS % lua_bytes_literal(source.encode("latin-1"))
    fd, path = tempfile.mkstemp(suffix=".luau", prefix="obfchk_")
    try:
        with os.fdopen(fd, "w", encoding="latin-1", newline="\n") as f:
            f.write(prog)
        out = subprocess.run([luau, path], capture_output=True, timeout=timeout)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    data = out.stdout.decode("latin-1")
    i = data.find("\1OUT\1")           # ignore any stray output before our marker
    data = data[i + 5:] if i >= 0 else "\0NO-OUTPUT\0" + data
    # error() embeds the chunk name + line into its message; both change with the source text
    # without any change in behaviour, so normalize them to compare the real message. Two forms:
    # our outer chunk (`obfchk:N:`) and an inner loadstring with the default name
    # (`[string "first line..."]:N:`, from the level-3 packer's own loadstring).
    data = re.sub(r'\[string "[^\n]*?"\]:\d+:', "<chunk>:#:", data)
    return re.sub(r"obfchk:\d+:", "<chunk>:#:", data)


def check(source, luau, levels=(1, 2, 3), seed=0):
    """Run the original and each level; return [(level, ok, detail)]."""
    base = run_capture(source, luau)
    results = [("orig", True, base)]
    for lv in levels:
        obf = obfuscate(source, level=lv, seed=seed)
        got = run_capture(obf, luau)
        results.append((lv, got == base, got))
    return results


def main():
    ap = argparse.ArgumentParser(description="Obfuscate a Luau script (test-sample generator).")
    ap.add_argument("input")
    ap.add_argument("-o", "--output")
    ap.add_argument("--level", type=int, default=3, choices=(1, 2, 3))
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--check", action="store_true", help="run the original and every level, diff the output")
    a = ap.parse_args()
    source = open(a.input, "rb").read().decode("latin-1")
    luau = harness.find_luau()
    if a.check:
        res = check(source, luau, seed=a.seed)
        base = res[0][2]
        print("original: %d bytes of output" % len(base))
        ok_all = True
        for lv, ok, got in res[1:]:
            print("level %s: %s" % (lv, "OK (identical output)" if ok else "DIFFERENT"))
            if not ok:
                ok_all = False
                print("  original:", repr(base[:200]))
                print("  obfusc. :", repr(got[:200]))
        sys.exit(0 if ok_all else 1)
    obf = obfuscate(source, level=a.level, seed=a.seed)
    if a.output:
        with open(a.output, "w", encoding="latin-1", newline="\n") as f:
            f.write(obf)
        print("[+] wrote %s (%d bytes, level %d)" % (a.output, len(obf), a.level), file=sys.stderr)
    else:
        sys.stdout.buffer.write(obf.encode("latin-1"))


if __name__ == "__main__":
    main()
