"""
Resolves references the dispatcher's leaves make to locals of the decoder layers wrapped around
ZEUS's own wrapper (model.Model.decoder_layers / outer_locals, see model.find_wrapper): some
builds add an outer layer that only decodes the wrapper call's own argument names and the few
strings used before the VM starts (a shuffled, custom-base64 string table + a decode-by-index
function), and the dispatcher's leaves keep calling that function as a Lua upvalue.

Rather than reimplementing the algorithm (random per build, like HYDRA's), the decoder layers'
own source runs under luau and every request collected while lifting a leaf
(model.Model.outer_requests: {declaration location: {arg tuple, ...}}) is answered by calling
the real function there. The request's result never reaches the final output as a function call:
lift.py substitutes the value directly once resolve() has run (see lift.py OuterRef, lift_program).
"""
import os
import subprocess
import tempfile

import harness
from obfuscators.zeus.model import _loc

SENT = "__ZEUS_OUTER__"
TIMEOUT = 30

HEAD = "local __hex = function(s) return (string.gsub(s, '.', function(c) " \
       "return string.format('%02x', string.byte(c)) end)) end\n"


def _luaval(x):
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, (int, float)):
        return repr(x)
    if isinstance(x, bytes):
        return _quote(x)
    raise TypeError("unsupported argument %r" % (x,))


def _quote(b):
    out = []
    for c in b:
        if 32 <= c < 127 and c not in (34, 92):
            out.append(chr(c))
        else:
            out.append("\\%03d" % c)
    return '"' + "".join(out) + '"'


def _prelude(model):
    """The decoder layers' own source, up to (not including) the statement that continues the
    chain to the next layer / the real ZEUS wrapper."""
    parts = []
    for fn, chain_stmt in model.decoder_layers:
        for s in fn["body"]["body"]:
            if s is chain_stmt:
                break
            parts.append(model.text(s))
    return "\n".join(parts)


def _probe(expr, idx):
    return ("do local __ok, __r = pcall(function() return %s end) local __k, __v = 'nil', '' "
            "if __ok then "
            "if type(__r) == 'string' then __k, __v = 's', __hex(__r) "
            "elseif type(__r) == 'number' then __k, __v = 'n', string.format('%%.17g', __r) "
            "elseif type(__r) == 'boolean' then __k, __v = 'b', (__r and '1' or '0') end end "
            "print('%s', %d, __ok and 1 or 0, __k, __v) end" % (expr, SENT, idx))


def _run(code, luau, debug=None):
    """Runs `code` (HEAD + prelude + probes) under luau: {probe index: value}."""
    if debug:
        with open(debug, "w", encoding="latin-1", newline="\n") as f:
            f.write(code)
    fd, path = tempfile.mkstemp(suffix=".luau", prefix="zouter_")
    try:
        with os.fdopen(fd, "w", encoding="latin-1", newline="\n") as f:
            f.write(code)
        try:
            out = subprocess.run([luau, path], capture_output=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            return {}
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    values = {}
    for line in out.stdout.decode("latin-1").splitlines():
        if not line.startswith(SENT):
            continue
        parts = line.split("\t")
        if len(parts) != 5:
            continue
        _, idx_s, ok_s, kind, val = parts
        if ok_s != "1":
            continue
        try:
            idx = int(idx_s)
        except ValueError:
            continue
        if kind == "s":
            values[idx] = bytes.fromhex(val)
        elif kind == "n":
            v = float(val)
            values[idx] = int(v) if v.is_integer() else v
        elif kind == "b":
            values[idx] = val == "1"
        elif kind == "nil":
            values[idx] = None
    return values


def resolve(model, luau=None, debug=None):
    """{declaration location: {arg tuple: value}}, answering every request in
    model.outer_requests; a request luau could not answer (it raised, or needs something only
    the real script environment has) is left out, so the caller's lift falls back to it."""
    if not model.outer_requests:
        return {}
    luau = luau or harness.find_luau()
    order = []
    calls = []
    for loc, argsets in model.outer_requests.items():
        name = model.outer_locals.get(loc)
        if not name:
            continue
        for args in sorted(argsets):
            idx = len(order)
            order.append((loc, args))
            calls.append(_probe("%s(%s)" % (name, ", ".join(_luaval(a) for a in args)), idx))
    if not calls:
        return {}
    values = _run(HEAD + _prelude(model) + "\n" + "\n".join(calls) + "\n", luau, debug)
    out_map = {}
    for idx, (loc, args) in enumerate(order):
        if idx in values:
            out_map.setdefault(loc, {})[args] = values[idx]
    return out_map


def resolve_exprs(model, luau=None, debug=None):
    """{call node location: value} for model.expr_requests (source text of calls into the decoder
    layers whose arguments are all constants, nested decoder calls included)."""
    todo = [(k, t) for k, t in model.expr_requests.items() if k not in model.expr_resolved]
    if not todo:
        return {}
    luau = luau or harness.find_luau()
    calls = [_probe(t, i) for i, (_, t) in enumerate(todo)]
    values = _run(HEAD + _prelude(model) + "\n" + "\n".join(calls) + "\n", luau, debug)
    return {todo[i][0]: v for i, v in values.items()}


def dump_consts(model, luau=None, debug=None):
    """The newer build's constant table, filled by the wrapper's own set-up code: run the script up to
    the wrapper's final `return` and read the table back. {key: number}."""
    luau = luau or harness.find_luau()
    ret = model.wrapper["body"]["body"][-1]
    l1, c1, _, _ = _loc(ret)
    model.text(ret)                               # (builds the line offset table)
    off = model._starts[l1] + c1
    probe = ("do local __t = {} for k, v in pairs(%s) do if type(k) == 'number' and type(v) == 'number' then "
             "print('%s', k, 1, 'n', string.format('%%.17g', v)) end end "
             "print('%s', 'done', 1, 'n', '0') error('__ZEUS_STOP__') end " % (model.consts_name, SENT, SENT))
    code = model.source[:off] + probe + model.source[off:]
    if debug:
        with open(debug, "w", encoding="latin-1", newline="\n") as f:
            f.write(code)
    fd, path = tempfile.mkstemp(suffix=".luau", prefix="zconsts_")
    try:
        with os.fdopen(fd, "w", encoding="latin-1", newline="\n") as f:
            f.write(code)
        try:
            out = subprocess.run([luau, path], capture_output=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    consts, done = {}, False
    for line in out.stdout.decode("latin-1").splitlines():
        parts = line.split("\t")
        if len(parts) != 5 or parts[0] != SENT:
            continue
        if parts[1] == "done":
            done = True
            continue
        v = float(parts[4])
        consts[int(float(parts[1]))] = int(v) if v.is_integer() else v
    return consts if done else None
