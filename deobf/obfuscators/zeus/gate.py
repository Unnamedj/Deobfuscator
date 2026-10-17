"""
ZEUS wraps the script in its own integrity gate: anti-tamper probes at the top (error-message line
number comparisons, random trials, a `_x ~= "ZEUS"` check), then `if <all probes passed> then <the
script> return ... end`, then a trap function. The lifted gate cannot pass outside the original file
(the probes compare line numbers that depend on the one-line layout), so the script is lifted out of
it. Only done when the script uses nothing the probes define.
"""
import re

from obfuscators.zeus import strings as S
from obfuscators.zeus.looptext import Walk, dedent, line_start


def globals_of(root):
    out = set()
    stack = [root]
    while stack:
        x = stack.pop()
        if isinstance(x, list):
            stack.extend(x)
        elif isinstance(x, dict):
            if x.get("type") == "AstExprGlobal":
                out.add(x["global"])
            stack.extend(v for v in x.values() if isinstance(v, (dict, list)))
    return out


def unwrap(text):
    """(text without the gate, True) or (text, False)."""
    data = text.encode("utf-8")
    src = S.Source(data)
    root = S.parse(data)
    body = root["body"]
    gate = None
    for i, st in enumerate(body):
        if st["type"] == "AstStatIf" and not st.get("elsebody"):
            inner = st["thenbody"]["body"]
            if inner and inner[-1]["type"] == "AstStatReturn":
                gate = i
    if gate is None:
        return text, False
    after = body[gate + 1:]
    if any(s["type"] not in ("AstStatLocalFunction", "AstStatReturn", "AstStatLocal") for s in after) or len(after) > 3:
        return text, False
    inner = list(body[gate]["thenbody"]["body"])
    # `if _x ~= "ZEUS" then return end`
    if inner and inner[0]["type"] == "AstStatIf" and not inner[0].get("elsebody"):
        c = inner[0]["condition"]
        t = inner[0]["thenbody"]["body"]
        if c["type"] == "AstExprBinary" and c["op"] == "CompareNe" and c["right"]["type"] == "AstExprConstantString" \
                and c["right"]["value"] == "ZEUS" and len(t) == 1 and t[0]["type"] == "AstStatReturn" \
                and not t[0]["list"]:
            inner = inner[1:]
    if not inner:
        return text, False
    w = Walk(root, src)
    lo = w.span(inner[0])[0]
    hi = w.span(inner[-1])[1]
    # the script must not read anything the probes declared
    for loc, refs in w.refs.items():
        m = re.match(r"(\d+),(\d+)", loc)
        dpos = src.starts[int(m.group(1))] + int(m.group(2))
        if dpos < lo and any(lo <= a < hi for a, _ in refs):
            return text, False
    new = dedent(data[line_start(src, lo):hi], 1)
    out = new.decode("utf-8")
    try:
        new_root = S.parse(out.encode("utf-8"))
    except ValueError:
        return text, False
    if globals_of(new_root) - globals_of(root):
        return text, False
    return out, True
