"""
In-memory async job runner: each job shells out to `deobf/deob.py` as its
own subprocess (the engine keeps global state between runs and its own
docs say to invoke it that way, never import it into a long-lived process).

A job's HTTP lifecycle is create -> poll: POST /api/jobs returns
immediately with a job id; the subprocess (which can take from seconds to
15+ minutes for a full devirtualization) runs in a background thread while
the frontend polls GET /api/jobs/{id}.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEOB_PY = os.path.join(REPO_ROOT, "deobf", "deob.py")
LUAU_BIN = os.path.join(REPO_ROOT, "deobf", "bin", "luau")

MAX_SOURCE_BYTES = 2_000_000
# Full devirtualization of a big script can take minutes; this is a last-resort
# kill switch so one stuck job can't hang the server forever.
HARD_KILL_SECONDS = 20 * 60

_JOBS = {}
_JOBS_LOCK = threading.Lock()


def engine_available():
    return os.path.isfile(DEOB_PY) and os.path.isfile(LUAU_BIN)


class Job:
    def __init__(self, filename, obfuscator, no_devirt, timeout, action):
        self.id = str(uuid.uuid4())
        self.filename = filename or "script.lua"
        self.obfuscator = obfuscator or "auto-detect"
        self.no_devirt = bool(no_devirt)
        self.timeout = int(timeout)
        self.action = action
        self.status = "pending"  # pending -> running -> done | error
        self.log = []
        self.output = None
        self.detected_obfuscator = None
        self.error = None
        self.created_at = time.time()
        self.finished_at = None
        self._lock = threading.Lock()

    def append_log(self, line):
        if not line:
            return
        with self._lock:
            self.log.append(line)

    def elapsed_ms(self):
        end = self.finished_at or time.time()
        return int((end - self.created_at) * 1000)

    def to_dict(self):
        with self._lock:
            log = list(self.log)
        return {
            "id": self.id,
            "action": self.action,
            "filename": self.filename,
            "status": self.status,
            "log": log,
            "output": self.output,
            "detectedObfuscator": self.detected_obfuscator,
            "error": self.error,
            "elapsedMs": self.elapsed_ms(),
        }


def create_job(source, filename, obfuscator, no_devirt, timeout, action):
    if len(source.encode("utf-8", errors="ignore")) > MAX_SOURCE_BYTES:
        raise ValueError("Script demasiado grande (máx %d bytes)." % MAX_SOURCE_BYTES)
    if action not in ("deobfuscate", "detect"):
        raise ValueError("action inválida.")

    job = Job(filename, obfuscator, no_devirt, timeout, action)
    with _JOBS_LOCK:
        _JOBS[job.id] = job
    threading.Thread(target=_run_job, args=(job, source), daemon=True).start()
    return job


def get_job(job_id):
    with _JOBS_LOCK:
        return _JOBS.get(job_id)


def _reader(stream, sink):
    try:
        for line in stream:
            sink(line.rstrip("\n"))
    except Exception:
        pass


def _run_job(job, source):
    from . import discord

    job.status = "running"
    discord.notify_started(job)

    workdir = tempfile.mkdtemp(prefix="deobjob_")
    stdout_lines = []
    try:
        in_path = os.path.join(workdir, job.filename)
        with open(in_path, "wb") as f:
            f.write(source.encode("latin-1", errors="replace"))

        out_path = os.path.join(workdir, "out.lua")
        cmd = [sys.executable, DEOB_PY, in_path, "--timeout", str(job.timeout)]
        if job.action == "detect":
            cmd.append("--detect")
        else:
            cmd += ["-o", out_path]
            if job.no_devirt:
                cmd.append("--no-devirt")
        if job.obfuscator and job.obfuscator != "auto-detect":
            cmd += ["--obfuscator", job.obfuscator]

        job.append_log("$ " + " ".join(os.path.basename(c) if c == DEOB_PY else c for c in cmd))

        proc = subprocess.Popen(
            cmd,
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        t_out = threading.Thread(
            target=_reader, args=(proc.stdout, lambda l: (stdout_lines.append(l), job.append_log(l)))
        )
        t_err = threading.Thread(target=_reader, args=(proc.stderr, job.append_log))
        t_out.start()
        t_err.start()

        try:
            code = proc.wait(timeout=HARD_KILL_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            code = -9
            job.append_log("[!] límite duro del servidor excedido (%ds): proceso terminado" % HARD_KILL_SECONDS)

        t_out.join(timeout=5)
        t_err.join(timeout=5)

        if job.action == "detect":
            if code == 0 and stdout_lines:
                parts = stdout_lines[-1].split("\t")
                job.detected_obfuscator = parts[0] if parts else None
                job.status = "done"
            else:
                job.status = "error"
                job.error = "La detección falló (código %s)." % code
        else:
            for line in job.log:
                m = re.match(r"^\[\*\] obfuscator: (.+?)(?: \(detected.*)?$", line)
                if m:
                    job.detected_obfuscator = m.group(1)
                    break
            if code == 0 and os.path.exists(out_path):
                with open(out_path, "r", encoding="utf-8", errors="replace") as f:
                    job.output = f.read()
                job.status = "done"
            else:
                job.status = "error"
                job.error = "El motor no produjo salida (código %s). Revisa el registro." % code
    except Exception as exc:  # noqa: BLE001
        job.status = "error"
        job.error = str(exc)
        job.append_log("[!] %s" % exc)
    finally:
        job.finished_at = time.time()
        shutil.rmtree(workdir, ignore_errors=True)
        try:
            from . import discord

            discord.notify_finished(job)
        except Exception:
            pass
