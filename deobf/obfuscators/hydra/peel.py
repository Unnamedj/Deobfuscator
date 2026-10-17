"""
Peel loadstring packer layers without executing the payload.

A layer decodes its payload with its own code (string tables, custom base64,
XOR/LCG passes whose constants all live in the file) and ends in
`loadstring(<decoded text>)()`. Instead of re-implementing each build's
decoder (names and formulas are randomised per build), the layer runs under
luau with `loadstring` replaced by a capture stub: the decoded text is
recorded and a no-op comes back, so the payload never runs.
"""
import os
import subprocess
import tempfile

import harness

SENT0 = "__DEOBF_PEEL_BEGIN_7f3a9b__"
SENT1 = "__DEOBF_PEEL_END_7f3a9b__"
MAX_LAYERS = 30
LAYER_TIMEOUT = 60
# a packer layer is mostly encoded data that expands to code; a script that
# merely contains `loadstring("...")` among lots of other code is not a layer
MIN_RATIO = 0.25
MIN_PAYLOAD = 20


def _quote(s):
    """Lua string literal for latin-1 text (decimal escapes: byte-exact, no long-string newline folding)."""
    out = []
    for ch in s:
        c = ord(ch)
        if 32 <= c < 127 and ch not in '"\\':
            out.append(ch)
        else:
            out.append("\\%03d" % c)
    return '"' + "".join(out) + '"'


def _harness(layer):
    return (
        "local __real = loadstring\n"
        "local __cap\n"
        "loadstring = function(s) if type(s) == 'string' then __cap = s end return function() end end\n"
        "local __f, __e = __real(%s, '=layer')\n"
        "if __f then pcall(__f) end\n"
        "print(%s .. (__cap or '') .. %s)\n"
    ) % (_quote(layer), _quote(SENT0), _quote(SENT1))


def peel_once(layer, luau=None, timeout=LAYER_TIMEOUT):
    """The payload (latin-1 text) the layer would pass to loadstring, or None
    when it isn't a packer layer (no loadstring call, not mostly data, timeout)."""
    if "loadstring" not in layer:
        return None
    luau = luau or harness.find_luau()
    fd, path = tempfile.mkstemp(suffix=".luau", prefix="peel_")
    try:
        with os.fdopen(fd, "w", encoding="latin-1", newline="\n") as f:
            f.write(_harness(layer))
        try:
            out = subprocess.run([luau, path], capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    data = out.stdout.decode("latin-1")
    i, j = data.find(SENT0), data.rfind(SENT1)
    if i < 0 or j < i:
        return None
    payload = data[i + len(SENT0):j]
    if len(payload) < MIN_PAYLOAD or len(payload) < MIN_RATIO * len(layer):
        return None
    return payload


def peel_all(source, luau=None, max_layers=MAX_LAYERS):
    """[layer 1 payload, layer 2 payload, ...]: every payload until one no longer unpacks."""
    layers = []
    cur = source
    while len(layers) < max_layers:
        payload = peel_once(cur, luau)
        if payload is None or payload == cur:
            break
        layers.append(payload)
        cur = payload
    return layers
