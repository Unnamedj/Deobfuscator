# obfuscate.py — test-sample generator (`deobf/research/obfuscate.py`)

> Reference, not changelog (same rules as CLAUDE.md). **Hard limit: 500 lines.**

Why it exists: we have obfuscated scripts for ZEUS and HYDRA but **no original+obfuscated
pairs with a known source**, so every semantic check against those is a behaviour trace, never
a diff. This tool makes pairs *with* a known source — for regression tests, and to exercise the
HYDRA peeler and the generic trace against input we fully control.

It is a **semantics-preserving source rewriter**, not a protection product. Every transform is
a local, provably-equivalent edit, and every change is validated by running the original and
the obfuscated side under `luau` and diffing their output (`--check`). It is deliberately
simple and readable; it is not meant to resist our own deobfuscator (the point is the opposite:
a known target to test it on). It operates on the luau-ast and applies byte-offset edits, like
the other tools in this repo (`instrument.py`, `looptext.py`).

## Levels (cumulative; `--level N` applies 1..N)

1. **String + integer-literal encoding.** Each string literal becomes `__S(i)` decoded by a
   small prelude (`__B` table of per-position-encoded bytes, `__S` reverses it). Each
   integer literal becomes `(A - B)` with `A - B` = the value. Record-style table keys
   (`{ name = v }`) are *not* literals and are left alone; only general keys `[expr]` and real
   values are touched. Non-integer / huge numbers are left alone.
2. **Local renaming.** Every distinct local declaration (keyed by its luau-ast location) gets
   one fresh unique name `_<hex>`, applied to the declaration token and every `AstExprLocal`
   that resolves to it. Unique names mean no shadowing or collision is possible; globals and
   fields are never renamed; `self` and `...` are left alone.
3. **Packer layer.** The whole transformed source (prelude included) is XOR'd with a 16-byte
   key and emitted as `local __e = "<bytes>" ... loadstring(decoded)(...)` — one HYDRA-style
   `loadstring` layer. `bit32.bxor`, not infix `~` (Luau has no infix bitwise operators).

## Usage

```
python research/obfuscate.py IN.lua -o OUT.lua [--level 1|2|3] [--seed N]
python research/obfuscate.py IN.lua --check         # run original + every level, diff output
```

`--check` prints `OK (identical output)` or `DIFFERENT` per level. It runs each side under luau
with `print` captured, comparing the captured output. `error()` messages embed the chunk name
and line (which change with the source text, not the behaviour), so the check normalizes
`obfchk:N:` and `[string "..."]:N:` location prefixes before comparing — that is the only
difference it is allowed to ignore.

## What the round-trip tests

- Level 3 output is detected as `hydra` and its layer is peeled by our own engine (the peeled
  payload still carries the level-1/2 encoding: revealing those is the deobfuscator's job via
  the trace, not the packer's). 
- Levels 1-2 output runs identically to the original but is not a recognized obfuscator, so
  `deob.py` falls back to the generic behaviour trace — a useful generic-path test.

## Gotchas / limits

- Decimal string escapes are always 3 digits (`\009`, not `\9`): `\9` + `2` would read back as
  byte 92. (CLAUDE.md warns about this; the tool does it right.)
- Integer literals only: floats and |v| ≥ 2^52 are left as-is (the `(A-B)` trick needs exact
  integer arithmetic).
- This is not control-flow flattening (no VM). A ZEUS-style flattener would be a much larger
  tool; the packer layer + encoding is enough to produce useful, controllable test input today.
- luau in this repo is sandboxed (no `io`): the `--check` harness uses `print`, not `io.write`.
