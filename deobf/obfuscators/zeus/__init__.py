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
TOP_RETURN = re.compile(r"^\s*return\s*\(\s*function\s*\(")
# a build that adds a name/string-decoding layer in front of the VM wrapper (model.find_wrapper
# follows it): the exact call-arg order/spelling varies, so just require this combination, rare
# together outside this family, before paying for the heavier structural check below
SIGNS = ("getfenv", "newproxy", "setmetatable", "getmetatable", "unpack")
MIN_SIZE = 4000
MIN_SIZE_ENCRYPTED = 100000
MIN_LEAVES = 50


def text_strings_done(prog):
    """Were string constants decrypted (so the decryptor's set-up code is dead)?"""
    return bool(getattr(prog, "strings", None))


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
        if len(source) < MIN_SIZE or not TOP_RETURN.match(source):
            return 0.0
        # (the newer build keeps every name encrypted, so the runtime names are not in the text:
        # there only `getfenv`, the dispatcher's `repeat ... until` and its size remain visible)
        if not all(s in source for s in SIGNS) and not ("getfenv" in source and "until" in source
                                                        and len(source) >= MIN_SIZE_ENCRYPTED):
            return 0.0
        # a decoder layer in front: confirm the real thing is in there by actually locating it
        try:
            from obfuscators.zeus import model as M
            m = M.Model(M.parse_source(source), source)
        except Exception:
            return 0.0
        return 0.85 if len(m.leaves) >= MIN_LEAVES else 0.0

    def add_arguments(self, ap):
        g = ap.add_argument_group("ZEUS")
        g.add_argument("--zeus-keep-strings", action="store_true",
                       help="leave the string constants encrypted (skip running the script's decryptor)")
        g.add_argument("--zeus-keep-protection", action="store_true",
                       help="keep ZEUS's own anti-tamper gate around the script")

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
        if not getattr(job.args, "zeus_keep_protection", False):
            text = backend.run_big_stack(self.ungate, text)
        text = backend.run_big_stack(self.loops, text)
        if text_strings_done(prog):
            text = backend.run_big_stack(self.dead_setup, text)
        text = backend.run_big_stack(lambda: backend.finish_text(backend.polish(text)))
        header = (job.credit_header() +
                  "-- Local names are inferred from use (the original names are not in the bytecode)\n")
        path = job.trace_path.replace(".deobf.luau", ".devirt.luau") if job.debug else job.trace_path
        job.write(path, header + "\n" + text + "\n")
        return path

    @staticmethod
    def ungate(text):
        """The script out of ZEUS's anti-tamper gate (gate.py)."""
        from obfuscators.zeus import gate
        try:
            new, done = gate.unwrap(text)
        except Exception as e:  # noqa: BLE001 - keep the gate
            print("[!] gate removal failed: %s: %s" % (type(e).__name__, e), file=sys.stderr)
            return text
        print("[*]   anti-tamper gate %s" % ("removed" if done else "kept (not the expected shape)"), file=sys.stderr)
        return new

    @staticmethod
    def dead_setup(text):
        """The string decryptor's set-up code, dead once the strings are decrypted (prelude.py)."""
        from obfuscators.zeus import prelude
        try:
            new, n = prelude.clean(text)
        except Exception as e:  # noqa: BLE001 - keep it
            print("[!] set-up removal failed: %s: %s" % (type(e).__name__, e), file=sys.stderr)
            return text
        print("[*]   %d dead set-up statements removed" % n, file=sys.stderr)
        return new

    @staticmethod
    def loops(text):
        """`while true do` loops back into `for` loops (looptext.py)."""
        from obfuscators.zeus import looptext
        try:
            new, n = looptext.rewrite(text)
        except Exception as e:  # noqa: BLE001 - keep the loops as they are
            print("[!] for-loop recovery failed: %s: %s" % (type(e).__name__, e), file=sys.stderr)
            return text
        print("[*]   %d loops written back as for" % n, file=sys.stderr)
        return new

    def fallback(self, job, why):
        import obfuscators
        print("[!] %s: writing the behaviour trace instead" % why, file=sys.stderr)
        return obfuscators.by_name("generic").deobfuscate(job)
