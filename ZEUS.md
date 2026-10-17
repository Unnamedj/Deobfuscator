# zeus (`deobf/obfuscators/zeus/`)

> Reference, not changelog (same rules as CLAUDE.md). **Hard limit: 500 lines.**

Header `("ZEUS"):gsub(".+",function(o)_x=o end)return(function(q,z,o,...)`, tail
`(newproxy,select,getfenv and getfenv()or _ENV,unpack or table.unpack,setmetatable,getmetatable,{...})`.
Status: **static devirtualizer** (the script is never run; only its string
decryptor is evaluated, in a sandbox). Fallback: generic trace. Names are
randomized per build, so every role is found by the shape of its code.
Sample: the user's `logic.txt` (178 KB, no source; not in `samples/`).

## How the obfuscation works

One wrapper function holds a few helpers and the **dispatcher** `F(state, args, cells, gc)`:
`while state do <if-tree comparing state with integer constants> end`. Every leaf
of the tree is a straight-line block over ~100 locals (the dispatcher's own
locals are the registers), assigning only `local = expr` statements; the last
assignment to the state register says where to go next (`model.py`):

- a number: the next block; `cond and A or B` (often built in two steps
  `F = c and A; ...; F = F or B`): a branch; a global read of a made-up name
  (`o.RandomName`): nil, the call ends and returns `unpack(<array register>)`.
- the state register is also used as plain data (blocks may read the incoming
  state, `Leaf.reads_state`).
- a Lua function = the blocks reachable from an entry state. Closures are
  `maker(entry state, {cells})` (the maker's parameter count is the arity;
  one maker is variadic); captured variables live in cells `w[id]` (`id = alloc()`),
  the closure's own cells are `N[i]`, arguments `q[i]`, `release(id)` ends a cell's scope.
- `S2[number]` (the first plain `{}` local) is a spill table: more registers.
- the constant table `local k = {[n] = number}` feeds `k[n] - c` state arithmetic.
- strings: `decrypt(assemble({order..., {pieces}}), key)`. `decrypt` caches the
  plaintext in a table a proxy `obj` reads (it returns the key: `obj[decrypt(..)]`).
- anti-tamper gate: probes comparing error-message line numbers (only valid in the
  one-line original), then `if <ok> then <script> return ... end` and a trap function.

## Pipeline

1. `model.py`: roles (`Special`, `Maker`), dispatcher leaves with state intervals.
2. `lift.py`: per function, sparse constant propagation over (leaf, incoming state
   if the leaf reads it); `LeafRun` evaluates a leaf. Abstract values: `Cell(site)`
   (captured variable -> `Reg(CELL_BASE+n)`), `UpCell(i)` (`Upval(i)`), `ARGS`
   (parameters; fixed arity -> `Vararg(i)` copied into param registers), `SPILL`
   (registers `SPILL_BASE+k`), `PackV` (a register holding `{a, f()}` with stable
   items: `t[k]` reads the item, the table goes dead), `V(e, t)` run-time values
   (`e` now, `t` the tree over incoming registers for the next-state decision).
   The next state is decided on `t` (`decide`); trees are written back as the
   register itself when it was not overwritten (`RegLeaf`), else rebuilt from its
   assigned tree when that reads no memory, else a copy is taken before the
   overwrite (`snap`, `before_write`). Result: `Node`s for `backend.lower`.
3. Strings: lift once, `strings.oracle(text)` (the lifted decryptor set-up runs
   under luau with a permissive environment, every `decrypt(...)` site is
   evaluated: `{key: (mode, plaintext)}`), lift again: calls with a known key
   become the key (mode "key"), `x[key]` the plaintext, the assembling calls are
   `droppable` (`codegen.CallE.droppable`: the back end drops them once unread).
4. Text passes before naming: `gate.py` (script out of the anti-tamper gate, only
   when it reads nothing the probes define), `looptext.py` (`while true` loops ->
   `for`), `prelude.py` (dead decryptor set-up). Then `backend.polish/finish_text`.

## Checks

- `compile_check`, `scope_check` (the trap function's `l1`/`l2` are the original's
  own undefined globals when the gate is kept), `active_locals` on the output.
- Behaviour: trace the original and the lifted output with `--obfuscator generic
  --no-devirt --no-fold` and the same `--cfg prelude=...` (the script checks
  `game.PlaceId`); the traces must match (logic.txt: identical).
- Options: `--zeus-keep-strings`, `--zeus-keep-protection`; `ZEUS_RAW=dir` writes
  each function's IR; `DEVIRT_TB=1` prints lifter tracebacks.

## Limits

- Not verified on other ZEUS builds (only one sample): the shapes above are what
  the code looks for; a different build raises `ModelError` / `Unsupported` and
  the plugin falls back to the trace (a leaf that cannot be lifted becomes an
  `error("devirt: ...")` line in its function).
- Lifting runs the lifter twice (strings): ~15 s for 180 KB.
- String sites whose arguments are not literal pieces after one lift stay encrypted.
- Loops with an `else return` exit instead of `break`, and dynamic-step numeric
  loops, stay `while`.
