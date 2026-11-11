# voltils (`deob/obfuscators/voltils.py`)

> Reference, not changelog (same rules as CLAUDE.md). **Hard limit: 500 lines.**

Status: **behaviour trace only** (the generic trace with `frame_pump` on). No lifter yet.

## What it is

`([[This file was protected using Voltils Obfuscation v7.4 - https://dsc.gg/Voltils]]):gsub(".+",function(v) getfenv()["__voltils_<id>"]=v end);return({...}):h(...)`,
one ~230 KB line: a table of ~80 functions (most are dead junk: tiny helpers with
constant arithmetic and `while` loops) plus two huge ones (`b`, `h`). Every string is built at
run time by decoder functions called with numeric constants (`s.d(0xB02,0x7F41)`,
`s.e(..)`, `s.l(..)`), so no API name appears in the text. Names are random per build.

## Environment check (why the plain trace died)

The script probes the fake environment first and spins (pure-VM endless loop, invisible to
the tracer, killed by the 20 s stall timeout) when something looks wrong. Found by running
the harness under a pty with `cfg ptrace=true` (stdout is block-buffered, a killed run loses
it: `script -qec "luau harness.luau" /dev/null` prints live):

1. Random-looking globals read for a free `nil` (`I7PUMPR0Yhdw`, `NFse7Liwuic2`,
   `qjwqDriBPzMZ`, `oofqqZyLzSmQ`): `CHAIN.canary` must say nil (rules in envlog.luau; a name
   with 2+ digits, or 5+ dense case flips, counts even with a lowercase stretch).
2. `RunService.RenderStepped:Connect(f)`, `task.wait(0.03)`, then "did f run?" (a `warn`
   "Voltis Anti-Env Logger ... RenderStepped did not fire" otherwise). Handlers normally run
   after the main script; `--cfg frame_pump=true` runs each connected frame handler
   (RenderStepped/Heartbeat/Stepped/Pre*/Post*) once per `task.wait`, the first run recorded as
   its body, later ones silent, `Disconnect()` stops it. The plugin turns it on.
3. `typeof(UserInputService:GetMouseLocation()) == "Vector2"`: now a Vector2 twin
   (960, 540); `GetMouseDelta` (0, 0).
4. `("%s"):format(proxy)` raised "string expected, got userdata" (the string metatable is
   frozen, so only the luau build can fix it: `build_luau.py` patch 2).

After these the script runs to the end (`run status: finished`, ~0.2 s).

## Sample

`samples/voltils-1.lua` (no source; `deob.py samples/voltils-1.lua`, ~1 s, must end with
`run status: finished` and list the three URLs). A Murder Mystery 2 **trade stealer** (analysis only): hides the `NewGui.TradeNegotiation`
window (`Visible = false`, `Position = UDim2.new(5,0,5,0)`, re-applied on change), mutes sounds,
hides notifications, reads the inventory (`PlayerData:TryIndex({"Inventory", ...})`), posts
`job_id`/`sender` to an API and an `@everyone` embed to a Discord webhook, exposes
`getgenv().script = {role="transfer", inventory, stats, unload}` and finally loadstrings
another hub (decoy). Data-dependent branches (what the victim owns) stay empty: the fake game
returns proxies for `TryIndex`.

## Open

- A real devirtualizer (decoder functions `s.d/e/l` evaluated statically, junk functions
  dropped) would show the untaken branches and every string; not started.
- Values that come from the game (inventory contents) are not modelled.
