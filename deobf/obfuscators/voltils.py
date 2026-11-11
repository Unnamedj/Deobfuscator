"""
Voltils plugin (notes: VOLTILS.md): behaviour trace only, no lifter. Voltils scripts carry an
environment check (a connected RenderStepped must fire within a moment, the mouse position must be a
Vector2, ...) and spin when the fake environment fails it, so the trace runs with the runtime's
frame pump on (`--cfg frame_pump=true`).
"""
import re

from obfuscators.generic import Generic

# header: `([[This file was protected using Voltils Obfuscation v7.4 - https://dsc.gg/Voltils]]):gsub(...)`
HEADER = re.compile(r"Voltils\s+Obfuscation", re.I)
KEY = "__voltils_"


class Voltils(Generic):
    name = "voltils"
    label = "Voltils (behaviour trace)"
    doc = "VOLTILS.md"
    cfg_defaults = {"frame_pump": True}

    def detect(self, source):
        head = source[:400]
        if HEADER.search(head) and KEY in source[:800]:
            return 1.0
        if HEADER.search(head):
            return 0.95
        return 0.0
