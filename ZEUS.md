# zeus (`deobf/obfuscators/zeus/`)

> Reference, not changelog (same rules as CLAUDE.md). **Hard limit: 500 lines.**

Header `("ZEUS"):gsub(".+",function(o)_x=o end)return(function(q,z,o,...)`, tail
`(newproxy,select,getfenv and getfenv()or _ENV,unpack or table.unpack,setmetatable,getmetatable,{...})`.
Status: **static devirtualizer** (the script is never run; only its string
decryptor and, in some builds, a name-decoding layer are evaluated, in a
sandbox). Fallback: generic trace. Names are randomized per build, so every
role is found by the shape of its code.
Samples: two user scripts (`logic.txt` 178 KB; `script_ofuscado.lua_2.txt`
274 KB, a build with the extra decoder layer below), both no source, neither
in `samples/`.

Some builds add an **outer decoder layer** wrapping the whole thing: `return
(function(...) local Q = {<shuffled, custom-base64 string pieces>} local
function q(k) return Q[k + OFFSET] end <shuffle loop> <base64 alphabet
do...end> return (<the wrapper above>)(<same 7 args, order/spelling varies>)
end)(...)`. The dispatcher's own leaves still call `q(k)` (and read `Q`
through it) as a plain Lua upvalue; `model.find_wrapper` follows the chain to
the real wrapper, `model.collect_outer_locals`/`decoders.py` resolve those
calls (see Pipeline). Also seen in this build: every numeric literal may be
written as a constant expression (`8712072-628598` instead of `7441474`,
or the exact value as the sample's own tamper check expects) --
`model.const_eval` folds `+ - * // %` of constants wherever a literal is
needed statically (dispatch thresholds, the entry state, the constant
table); arbitrary ones inside a leaf fold naturally during lift (it
evaluates expressions, not just pattern-matches them).

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

1. `model.py`: `find_wrapper` follows decoder layers (above) to the real one;
   roles (`Special`, `Maker`); dispatcher leaves with state intervals
   (`const_eval` where a literal is expected statically).
2. Outer decoder calls (`model.outer_locals`/`outer_requests`): `model.
   prescan_outer_calls` finds every constant-argument call of one of those
   locals anywhere in the file upfront; `decoders.py` answers them all in one
   luau run before lifting starts (the decoder layers' own source, verbatim,
   up to the statement that continues the chain). A leaf can still ask for
   more (a call the prescan missed): `lift.py`'s `OuterRef`/`Pending` let the
   rest of that leaf's statements lift anyway (only reading the pending
   value concretely raises `Unsupported`), so unrelated requests in the same
   leaf surface in the same round; `lift_program` resolves a growing batch
   and relifts, up to `MAX_DECODER_ROUNDS`.
3. `lift.py`: per function, sparse constant propagation over (leaf, incoming state
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
4. Strings: lift once, `strings.oracle(text)` (the lifted decryptor set-up runs
   under luau with a permissive environment, every `decrypt(...)` site is
   evaluated: `{key: (mode, plaintext)}`), lift again: calls with a known key
   become the key (mode "key"), `x[key]` the plaintext, the assembling calls are
   `droppable` (`codegen.CallE.droppable`: the back end drops them once unread).
   `strings.cut_after_setup` locates the decryptor set-up by scanning *into*
   if/while/for/repeat/block statements (`find_cut`/`_block_children`), not just
   a bare `do...end`: the whole script can sit inside one `if <self test
   passed> then ... end` with no separate closure. Before this scan, `gate.
   unwrap` forces that gate open (a copy, only for the scan: the real anti-tamper
   gate stays until step 5 removes it) -- the probes would otherwise never run.
5. Text passes before naming: `gate.py` (script out of the anti-tamper gate, only
   when it reads nothing the probes define), `looptext.py` (`while true` loops ->
   `for`), `prelude.py` (dead decryptor set-up). Then `backend.polish/finish_text`.

## Detection

Header/tail regex for the plain shape (1.0); for the decoder-layer build
(no literal `"ZEUS"` string, call-arg order/spelling varies): a cheap shape
gate (`return(function(` + `getfenv`/`newproxy`/`setmetatable`/`getmetatable`/
`unpack` all present + size), then an actual `model.Model(...)` build from
the in-memory source (`model.parse_source`, no file needed) scored on leaf
count (0.85) -- a real structural check, not a guess, since the regex can't
pin down this family's varying shape.

## Checks

- `compile_check`, `scope_check` (the trap function's `l1`/`l2` are the original's
  own undefined globals when the gate is kept), `active_locals` on the output.
- Behaviour: trace the original and the lifted output with `--obfuscator generic
  --no-devirt --no-fold` and the same `--cfg prelude=...` (the script checks
  `game.PlaceId`); the traces must match (both samples: identical).
- Options: `--zeus-keep-strings`, `--zeus-keep-protection`; `ZEUS_RAW=dir` writes
  each function's IR; `DEVIRT_TB=1` prints lifter tracebacks;
  `ZEUS_DECODER_DEBUG=file` saves the outer-decoder resolver's harness.

## Limits

- Only two samples, no sources for either: the shapes above are what the code
  looks for; a different build raises `ModelError` / `Unsupported` and the
  plugin falls back to the trace (a leaf that cannot be lifted becomes an
  `error("devirt: ...")` line in its function).
- Lifting runs the lifter multiple times (outer-decoder rounds, then strings):
  ~5-9 s for both samples (150-280 KB); `MAX_DECODER_ROUNDS` bounds the rounds.
- An outer-decoder call with a non-constant argument stays as `error(...)`.
- String sites whose arguments are not literal pieces after one lift stay
  encrypted; so do byte strings that were never behind `decrypt()` or the
  outer decoder to begin with (an obfuscator can use raw non-ASCII bytes as
  ordinary table keys on purpose, not a bug to chase).
- Loops with an `else return` exit instead of `break`, and dynamic-step numeric
  loops, stay `while`. `gate.unwrap`/the loop and prelude passes only handle a
  plain `do...end`/`if...end` shape, not an `elseif` chain straddling the cut.
