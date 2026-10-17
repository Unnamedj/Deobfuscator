"""
hydra: loadstring packers (HYDRA and look-alikes). Notes: HYDRA.md. Each layer
is a data blob plus a decoder ending in `loadstring(decoded)()`; peel.py runs
the decoder with a capture stub (the payload never runs) until the payload no
longer unpacks. Then the payload goes to whichever plugin recognizes it
(Luraph, IronBrew, ...); plain source is written out as it is.
"""
import re
import sys

import harness
from obfuscators.base import Obfuscator, Job
from obfuscators.hydra import peel

# HYDRA PRIVATE names its tables _HY (string pieces), _HX (order), _HA (alphabet)
MARKERS = re.compile(r"\b_HY\s*=\s*\{")
HEADER = re.compile(r"hydra", re.I)
LOAD_AT_END = re.compile(r"\b(?:loadstring|load)\s*\(")
STRING_LITERAL = re.compile(r'"(?:[^"\\\n]|\\.)*"')
MIN_SHAPE_SIZE = 600
MAX_FUNCTIONS = 8


class Hydra(Obfuscator):
    name = "hydra"
    label = "HYDRA packer (loadstring layers)"
    doc = "HYDRA.md"

    def detect(self, source):
        if MARKERS.search(source) and "_HX" in source:
            return 0.95
        if HEADER.search(source[:400]):
            return 0.9
        # build-independent shape: mostly string data, almost no code, ending in a loadstring call
        if len(source) < MIN_SHAPE_SIZE or not LOAD_AT_END.search(source[-800:]):
            return 0.0
        if source.count("function") > MAX_FUNCTIONS:
            return 0.0
        data = sum(len(m) for m in STRING_LITERAL.findall(source))
        return 0.6 if data >= 0.35 * len(source) else 0.0

    def deobfuscate(self, job):
        import obfuscators
        print("[*] unpacking layers...", file=sys.stderr)
        layers = peel.peel_all(job.source, harness.find_luau())
        if not layers:
            print("[!] no loadstring layer could be unpacked; tracing the script instead", file=sys.stderr)
            return obfuscators.by_name("generic").deobfuscate(job)
        payload = layers[-1]
        n = len(layers)
        suffix = "inside %d packer layer%s" % (n, "" if n == 1 else "s")
        print("[*] %d layer%s unpacked (%d bytes)" % (n, "" if n == 1 else "s", len(payload)), file=sys.stderr)
        if job.debug:
            for i, layer in enumerate(layers, 1):
                job.write(job.path(".layer%d.lua" % i), layer, encoding="latin-1")
        inner = max((cp for cp in obfuscators.scores(payload) if cp[1].name != self.name),
                    key=lambda cp: cp[0])
        conf, plugin = inner
        if conf < obfuscators.MIN_CONFIDENCE or plugin.name == "generic":
            return self.write_source(job, payload, n)
        print("[*] obfuscator: %s (detected, %.2f) %s" % (plugin.label, conf, suffix), file=sys.stderr)
        path = job.path(".unpacked.lua")
        job.write(path, payload, encoding="latin-1")
        sub = Job(path, payload, job.args, job.trace_path, job.debug, "%s (%s)" % (plugin.label, suffix))
        return plugin.deobfuscate(sub)

    def write_source(self, job, payload, n):
        """The payload isn't obfuscated any further: it is the original script."""
        plural = "" if n == 1 else "s"
        job.obfuscator = "%s, %d layer%s" % (self.label, n, plural)
        text = job.credit_header()
        text += "-- unpacked from %d loadstring layer%s; nothing was executed\n\n" % (n, plural)
        text += payload.replace("\r\n", "\n")
        return job.write(job.trace_path, text, encoding="latin-1")
