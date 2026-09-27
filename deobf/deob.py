"""
Placeholder for the real deobfuscation engine.

Replace this file (and add any supporting modules under deobf/) with your
actual deob.py logic — the same one deobf_gui.pyw calls as a CLI
subprocess. The web backend (api/deobfuscate.py) calls it directly as a
Python function instead of shelling out:

    from deobf import deob
    deob.run(source=<str>, obfuscator=<str>, no_devirt=<bool>, timeout=<int>)

Expected return shape:

    {
        "output": <str>,                # deobfuscated source
        "log": [<str>, ...],            # lines of log/trace output
        "detected_obfuscator": <str>,   # e.g. "luraph_v15", "ironbrew1", "generic"
    }

`obfuscator` is "auto-detect" or one of the known obfuscator names.
`no_devirt` mirrors the CLI's --no-devirt (fast/trace-only) flag.
`timeout` is a soft budget in seconds; raise on your own if you exceed it.

Until this raises something other than NotImplementedError, the web app
falls back to a labeled demo-only transformation and shows a warning
banner in the UI.
"""


def run(source: str, obfuscator: str = "auto-detect", no_devirt: bool = False, timeout: int = 90):
    raise NotImplementedError(
        "Motor real no conectado: reemplaza deobf/deob.py con tu implementación."
    )
