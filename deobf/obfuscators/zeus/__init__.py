"""
ZEUS plugin: a Luau obfuscator that flattens the script into one giant state-machine function
(notes: ZEUS.md). The lifter (lift.py) reads it statically: the script is never run, only the
string decryptor it carries is evaluated in a sandbox (strings.py). The behaviour trace is the
fallback when the structure is not the expected one.
"""
import re
import sys
import time

import backend
from obfuscators.base import Obfuscator

# header: `("ZEUS"):gsub(".+",function(o)_x=o end)return(function(...` ; tail: the arguments the
# wrapper receives (the runtime functions it needs)
HEAD = re.compile(r'\s*\(\s*"ZEUS"\s*\)\s*:\s*gsub\(')
TAIL = re.compile(r"\(\s*newproxy\s*,\s*select\s*,\s*getfenv and getfenv\(\)\s*or _ENV\s*,\s*unpack or table\.unpack"
                  r"\s*,\s*setmetatable\s*,\s*getmetatable\s*,\s*\{\s*\.\.\.\s*\}\s*\)\s*$")


class Zeus(Obfuscator):
    name = "zeus"
    label = "ZEUS"
    doc = "ZEUS.md"

    def detect(self, source):
        head = HEAD.match(source[:200]) is not None
        tail = TAIL.search(source[-400:]) is not None
        if head and tail:
            return 1.0
        if tail and source.lstrip().startswith("return(function("):
            return 0.9
        return 0.0

    def add_arguments(self, ap):
        g = ap.add_argument_group("ZEUS")
        g.add_argument("--zeus-keep-strings", action="store_true",
                       help="leave the string constants encrypted (skip running the script's decryptor)")

    def deobfuscate(self, job):
        args = job.args
        if args.no_devirt:
            return self.fallback(job, "--no-devirt")
        try:
            return self.lift(job)
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001 - never lose the run over a lifter bug
            import os
            if os.environ.get("DEVIRT_TB"):
                import traceback
                traceback.print_exc()
            print("[!] devirtualization failed: %s: %s" % (type(e).__name__, e), file=sys.stderr)
            return self.fallback(job, "the lifter failed")

    def lift(self, job):
        from obfuscators.zeus import model, lift
        t0 = time.time()
        print("[*] devirtualizing (static; the script is not run)...", file=sys.stderr)
        m = model.load(job.source_path)
        print("[*]   %d blocks, main function at state %d" % (len(m.leaves), m.entry), file=sys.stderr)
        text, prog = backend.run_big_stack(lift.lift_program, m, not getattr(job.args, "zeus_keep_strings", False))
        st = prog.stats
        print("[*]   %d functions, %d unlifted blocks, %d unstructured jumps (%.1fs)"
              % (st["functions"], st["errors"], st["fallbacks"], time.time() - t0), file=sys.stderr)
        text = backend.run_big_stack(lambda: backend.finish_text(backend.polish(text)))
        header = (job.credit_header() +
                  "-- Local names are inferred from use (the original names are not in the bytecode)\n")
        path = job.trace_path.replace(".deobf.luau", ".devirt.luau") if job.debug else job.trace_path
        job.write(path, header + "\n" + text + "\n")
        return path

    def fallback(self, job, why):
        import obfuscators
        print("[!] %s: writing the behaviour trace instead" % why, file=sys.stderr)
        return obfuscators.by_name("generic").deobfuscate(job)
