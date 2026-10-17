# hydra (`deobf/obfuscators/hydra/`)

> Reference, not changelog (same rules as CLAUDE.md). **Hard limit: 500 lines.**

Status: **unpacker** (no lifter). HYDRA PRIVATE and look-alikes are *source
packers*: each layer is string tables plus a custom base64 and XOR/LCG passes
whose constants all live in the file, ending in `loadstring(decoded)()`. No
key, no server, so the payload is recoverable offline. Several layers nest.
Credit: the approach (run the layer's own decoder with `loadstring` captured)
comes from the HYDRA unpacker scripts the user supplied (`unhydra.py`,
`peel.py`, `hydra_unpack.py`); the code here is our own.

## Pipeline

1. `peel.py`: `peel_once(layer)` runs the layer under luau with `loadstring`
   replaced by a stub (records the last string argument, returns a no-op), so
   the payload never runs. The layer is compiled with the real `loadstring`
   from a decimal-escaped literal (byte exact). `peel_all` repeats until a
   payload no longer unpacks (max 30 layers, 60 s each).
2. A result counts as a layer only when the payload is >= 20 bytes and >= 25 %
   of the layer's size (a script that merely calls `loadstring("...")` among
   other code is not a packer). Else the plugin falls back to the generic trace.
3. `__init__.py`: the final payload goes to the best other plugin
   (`obfuscators.scores`, confidence >= 0.5, not generic): Luraph / IronBrew
   run on it as a normal `Job` (label "Luraph v15 (inside 2 packer layers)").
   Otherwise the payload *is* the original script and is written as it is
   (credit header + a note), not traced.
4. `--debug` also writes every layer (`<name>.layerN.lua`).

## Detection

- `_HY = {` + `_HX` (HYDRA's table names): 0.95; the word "hydra" in the first
  400 bytes: 0.9.
- Build-independent shape (names are randomised per build): >= 600 bytes, a
  `loadstring(`/`load(` call in the last 800 bytes, <= 8 `function` keywords,
  string literals >= 35 % of the file: 0.6. A wrong guess costs one
  luau run, then the generic trace. VM obfuscators score higher on their own.

## Tests

No real HYDRA sample is in the repo (the supplied zip had none). The layers
were generated with a Python packer that mirrors `unhydra.py`'s maths
(custom base64, two chained XOR/LCG passes, checksums, shuffled string pieces,
random names): 1 and 3 layers around a plain script, 2 layers around
`001_vm_like_dispatch-obfuscated.lua` (the unpacked bytes equal the original
byte for byte, the devirtualized output equals the direct run's). If a real
HYDRA file fails, add it to `samples/` and check what the capture stub misses
(e.g. a layer that calls `loadstring` on its own decoder first: the last
call wins).

## Limits

- Anything that needs a Roblox environment to decode (`game`, `getgenv`) fails
  in plain luau and falls back to the trace. Add an environment-backed capture
  (run the layer in `envlog`) if that shows up.
- Bytecode payloads (`\27Lua...`) are not handled.
