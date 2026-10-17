"""
ZEUS string decryption oracle.

Every string constant of a ZEUS script is a call `decrypt(assemble({order..., {pieces}}), key)`
(or `decrypt("short", key)`); `decrypt` returns the key and caches the plaintext in a table
that a proxy `obj` reads (`obj[decrypt(...)]`). The decryptor is built by the script itself
when its main function starts, so it is evaluated, not re-implemented (its algorithm and
constants change with every build): the lifted function body is cut after the statements that
set up `decrypt`, `assemble` and `obj`, run under luau with a permissive environment (unknown
globals answer with an inert proxy), and every call site is evaluated there.

oracle(text) -> {key: plaintext bytes} for the keys it could decrypt, plus how the call answers:
  "key"  the call returns its key and `obj[key]` is the plaintext
  "str"  the call returns the plaintext
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter

import harness

TIMEOUT = 30
MIN_SITES = 3
SENT = "__ZEUS_STR__"


def parse(text_bytes):
    fd, path = tempfile.mkstemp(suffix=".luau", prefix="zstr_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text_bytes)
        out = subprocess.run([harness.luau_ast(), path], capture_output=True)
    finally:
        os.remove(path)
    text = out.stdout.decode("latin-1")
    start = text.find('{"root"')
    if start < 0:
        raise ValueError("luau-ast: " + text[:200])
    return json.loads(text[start:])["root"]


def span(n):
    m = re.match(r"(\d+),(\d+) - (\d+),(\d+)", n["location"])
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))


class Source:
    def __init__(self, data):
        self.data = data
        self.starts = [0]
        for m in re.finditer(b"\n", data):
            self.starts.append(m.end())

    def text(self, n):
        l1, c1, l2, c2 = span(n)
        return self.data[self.starts[l1] + c1:self.starts[l2] + c2]

    def start_of(self, n):
        l1, c1, _, _ = span(n)
        return self.starts[l1] + c1

    def end_of(self, n):
        _, _, l2, c2 = span(n)
        return self.starts[l2] + c2


class Index:
    """One walk over the AST: parents, owner functions of local declarations, decryptor call sites."""

    def __init__(self, root):
        self.root = root
        self.parent = {}
        self.owner = {}         # declaration location -> function node (or root) declaring it
        self.calls = []
        self.func_of = {}       # id(node) -> enclosing function node
        self.localdef = {}      # declaration location -> initializer of `local x = <expr>`
        self.declname = {}      # declaration location -> name
        self.nassign = Counter()    # declaration location -> assignments after the declaration
        stack = [(root, None, root)]
        while stack:
            n, par, fn = stack.pop()
            if isinstance(n, list):
                for x in n:
                    stack.append((x, par, fn))
                continue
            if not isinstance(n, dict):
                continue
            if "type" in n:
                self.parent[id(n)] = par
                self.func_of[id(n)] = fn
                t = n["type"]
                if t == "AstExprFunction":
                    for a in n["args"]:
                        self.owner[a["location"]] = n
                        self.declname[a["location"]] = a["name"]
                    cur = n
                else:
                    cur = fn
                if t == "AstStatLocal":
                    for v in n["vars"]:
                        self.owner[v["location"]] = fn
                        self.declname[v["location"]] = v["name"]
                    if len(n["vars"]) == 1 and len(n["values"]) == 1:
                        self.localdef[n["vars"][0]["location"]] = n["values"][0]
                elif t == "AstStatAssign":
                    for v in n["vars"]:
                        if v["type"] == "AstExprLocal":
                            self.nassign[v["local"]["location"]] += 1
                elif t == "AstStatCompoundAssign":
                    if n["var"]["type"] == "AstExprLocal":
                        self.nassign[n["var"]["local"]["location"]] += 1
                elif t == "AstStatLocalFunction":
                    self.owner[n["name"]["location"]] = fn
                    self.declname[n["name"]["location"]] = n["name"]["name"]
                elif t in ("AstStatFor",):
                    self.owner[n["var"]["location"]] = fn
                elif t == "AstStatForIn":
                    for v in n["vars"]:
                        self.owner[v["location"]] = fn
                elif t == "AstExprCall":
                    self.calls.append(n)
                for k, v in n.items():
                    if isinstance(v, (dict, list)):
                        stack.append((v, n, cur))
            else:
                for k, v in n.items():
                    if isinstance(v, (dict, list)):
                        stack.append((v, par, fn))


def local_ref(n):
    if n["type"] == "AstExprLocal":
        return n["local"]["location"], n["local"]["name"]
    return None


def writes_of(stmt):
    """Declaration locations a statement (anywhere inside it) declares or assigns."""
    out = set()
    stack = [stmt]
    while stack:
        n = stack.pop()
        if isinstance(n, list):
            stack.extend(n)
            continue
        if not isinstance(n, dict):
            continue
        t = n.get("type")
        if t == "AstStatLocal":
            out.update(v["location"] for v in n["vars"])
        elif t == "AstStatLocalFunction":
            out.add(n["name"]["location"])
        elif t == "AstStatAssign":
            for v in n["vars"]:
                r = local_ref(v)
                if r:
                    out.add(r[0])
        elif t == "AstStatCompoundAssign":
            r = local_ref(n["var"])
            if r:
                out.add(r[0])
        for v in n.values():
            if isinstance(v, (dict, list)):
                stack.append(v)
    return out


class Site:
    __slots__ = ("node", "callee", "callee_name", "key", "arg0", "assemble", "obj")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


def canon(ix, ref):
    """(location, name) of the variable a local is only a copy of: `local a = b` -> b."""
    loc, name = ref
    for _ in range(8):
        d = ix.localdef.get(loc)
        if d is None or ix.nassign[loc] or d["type"] != "AstExprLocal":
            break
        loc = d["local"]["location"]
        name = ix.declname.get(loc, d["local"]["name"])
    return loc, ix.declname.get(loc, name)


def piece(ix, node, depth=0):
    """A decryptor's first argument as (assemble variable | None, source node of the string or table)."""
    if node["type"] == "AstExprLocal" and depth < 3:
        loc = node["local"]["location"]
        d = ix.localdef.get(loc)
        if d is not None and not ix.nassign[loc]:
            return piece(ix, d, depth + 1)
        return None
    if node["type"] == "AstExprConstantString":
        return None, node
    if node["type"] == "AstExprCall" and len(node["args"]) == 1 and node["args"][0]["type"] == "AstExprTable":
        f = local_ref(node["func"])
        if f is not None:
            return canon(ix, f), node["args"][0]
    return None


def find_sites(ix, src):
    """Calls shaped decrypt(<string | assemble({...})>, <number>), through local copies."""
    sites = []
    for c in ix.calls:
        ref = local_ref(c["func"])
        if ref is None or len(c["args"]) != 2:
            continue
        a0, a1 = c["args"]
        if a1["type"] != "AstExprConstantNumber":
            continue
        p = piece(ix, a0)
        if p is None:
            continue
        asm, lit = p
        par = ix.parent.get(id(c))
        obj = None
        if par is not None and par["type"] == "AstExprIndexExpr" and par["index"] is c:
            r = local_ref(par["expr"])
            obj = canon(ix, r) if r else None
        root = canon(ix, ref)
        sites.append(Site(node=c, callee=root[0], callee_name=root[1], key=int(a1["value"]), arg0=src.text(lit),
                          assemble=asm, obj=obj))
    return sites


def run_chunk(code, luau, timeout):
    fd, path = tempfile.mkstemp(suffix=".luau", prefix="zstr_run_")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(code)
        out = subprocess.run([luau, path], capture_output=True, timeout=timeout)
    finally:
        os.remove(path)
    return out


HARNESS_HEAD = rb'''
local __real = getfenv(0)
local __P
__P = setmetatable({}, {
	__index = function() return __P end, __newindex = function() end, __call = function() return __P end,
	__concat = function() return __P end, __len = function() return 0 end, __unm = function() return __P end,
	__add = function() return __P end, __sub = function() return __P end, __mul = function() return __P end,
	__div = function() return __P end, __mod = function() return __P end, __pow = function() return __P end,
	__tostring = function() return "proxy" end,
})
local __env = setmetatable({}, {__index = function(_, k) local v = __real[k] if v ~= nil then return v end return __P end,
	__newindex = function(_, k, v) rawset(__real, k, v) end})
local __print = print
local __hex = function(s) return (string.gsub(s, ".", function(c) return string.format("%02x", string.byte(c)) end)) end
local __emit = function(k, kind, v) __print("''' + SENT.encode() + rb'''", k, kind, type(v) == "string" and __hex(v) or tostring(v)) end
local function __main()
'''

HARNESS_TAIL = rb'''
end
setfenv(__main, __env)
local __ok, __err = pcall(__main)
if not __ok then __print("''' + SENT.encode() + rb'''", "run", tostring(__err)) end
'''


def _block_children(st):
    """A compound statement's own nested statement list(s) that keep executing in source order
    (block/if/while/for/repeat): the setup can sit inside one of these instead of directly in
    the function body (e.g. a top-level `if <self test passed> then <the whole script> end`).
    An `elseif` continuation is skipped (it would need its own, trickier closing text: Luau
    desugars it into a nested AstStatIf that shares the outer one's single source-level `end`)."""
    t = st["type"]
    if t == "AstStatBlock":
        return [st["body"]]
    if t in ("AstStatWhile", "AstStatRepeat", "AstStatFor", "AstStatForIn"):
        return [st["body"]["body"]]
    if t == "AstStatIf":
        out = [st["thenbody"]["body"]]
        e = st.get("elsebody")
        if e is not None and e["type"] != "AstStatIf":
            out.append(e["body"])
        return out
    return []


def _closer(st, src):
    """The source text that closes a compound statement found open at a cut point."""
    if st["type"] == "AstStatRepeat":
        return b"\nuntil " + src.text(st["condition"])
    return b"\nend"


def _writes_any(stmts, watch):
    return any(writes_of(st) & watch for st in stmts)


def find_cut(stmts, watch):
    """(path, statement) of the deepest, latest-in-source statement in `stmts` (recursively,
    through _block_children) that writes a watched location; `path`: the compound statements
    (outermost first) still open around it, each needing its own closing text. None if nothing
    matches anywhere."""
    found = [None]

    def visit(lst, path):
        for st in lst:
            if not (writes_of(st) & watch):
                continue
            kids = [c for c in _block_children(st) if _writes_any(c, watch)]
            if kids:
                for child in kids:
                    visit(child, path + [st])
            else:
                found[0] = (path, st)          # later in source order: overwrites any earlier one
    visit(stmts, [])
    return found[0]


def cut_after_setup(stmts, watch, tail, src):
    """The source text of a body up to and including the last statement (however deeply nested
    in an if/while/for/repeat/block) that writes a watched variable, with `tail` right after it
    and every compound statement still open at that point properly closed (the script's own
    code after the setup, such as early returns, must not run before `tail`)."""
    if not stmts:
        return None
    found = find_cut(stmts, watch)
    if found is None:
        return None
    path, leaf = found
    prefix = src.data[src.start_of(stmts[0]):src.end_of(leaf)]
    closers = b"".join(_closer(st, src) for st in reversed(path))
    return prefix + b"\n" + tail + closers


def oracle(text, luau=None, debug=None):
    """{key: (mode, plaintext)} for the string decryptors of the script text (str); {} when
    there are none or they could not be evaluated. mode "key": the decryptor returns its key and
    `obj[key]` is the plaintext; "str": it returns the plaintext."""
    luau = luau or harness.find_luau()
    data = text.encode("utf-8")
    src = Source(data)
    ix = Index(parse(data))
    sites = find_sites(ix, src)
    groups = Counter(s.callee for s in sites)
    result = {}
    for callee, n in groups.most_common():
        if n < MIN_SITES:
            break
        got = run_group(ix, src, [s for s in sites if s.callee == callee], callee, luau,
                        debug + ".%d" % len(result) if debug else None)
        result.update(got)
    return result


def run_group(ix, src, group, callee, luau, debug):
    callee_name = group[0].callee_name
    asm = Counter(s.assemble for s in group if s.assemble)
    obj = Counter(s.obj for s in group if s.obj)
    watch = {callee}
    obj_ref = obj.most_common(1)[0][0] if obj else None
    for r in (asm.most_common(1)[0][0] if asm else None, obj_ref):
        if r:
            watch.add(r[0])
    owner = ix.owner.get(callee)
    if owner is None:
        return {}
    body = owner["body"]["body"] if owner["type"] == "AstExprFunction" else owner["body"]
    keys = {}
    for s in group:
        keys.setdefault(s.key, s)
    asm_name = asm.most_common(1)[0][0][1].encode() if asm else None
    ev = []
    for k, s in keys.items():
        if s.assemble:
            if asm_name is None:
                continue
            s.arg0 = b"%s(%s)" % (asm_name, s.arg0)
        ev.append(b"do local ok, kind, v = pcall(function() local r = %s(%s, %d) "
                  b"if r == %d and %s then return 'key', %s[%d] end return 'str', r end) "
                  b"if ok then __emit(%d, kind, v) else __emit(%d, 'error', kind) end end"
                  % (callee_name.encode(), s.arg0, k, k, obj_ref[1].encode() if obj_ref else b"nil",
                     obj_ref[1].encode() if obj_ref else b"nil", k, k, k))
    prefix = cut_after_setup(body, watch, b"\n".join(ev), src)
    if prefix is None:
        return {}
    code = HARNESS_HEAD + prefix + HARNESS_TAIL
    if debug:
        with open(debug, "wb") as f:
            f.write(code)
    try:
        out = run_chunk(code, luau, TIMEOUT)
    except subprocess.TimeoutExpired:
        print("[!] string decryptor timed out", file=sys.stderr)
        return {}
    result = {}
    for line in out.stdout.decode("latin-1").splitlines():
        if not line.startswith(SENT):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        if parts[1] in ("compile", "run"):
            print("[!] string decryptor %s error: %s" % (parts[1], "\t".join(parts[2:])[:200]), file=sys.stderr)
            continue
        if len(parts) < 4:
            continue
        key, kind, val = parts[1], parts[2], parts[3]
        if kind in ("key", "str"):
            try:
                result[int(key)] = (kind, bytes.fromhex(val))
            except ValueError:
                pass
    if not result:
        err = out.stderr.decode("latin-1")[:300]
        if err:
            print("[!] string decryptor: %s" % err, file=sys.stderr)
    return result
