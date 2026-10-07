"""
In-memory async job runner: each job shells out to `deobf/deob.py` as its
own subprocess (the engine keeps global state between runs and its own
docs say to invoke it that way, never import it into a long-lived process).

POST /api/jobs returns immediately with a job id; the subprocess (seconds
to 15+ minutes for a full devirtualization) runs in a background thread
while the frontend polls GET /api/jobs/{id}. At most MAX_CONCURRENT_JOBS
run at once; the rest wait in a FIFO queue.
"""

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from . import discord, fetch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEOB_PY = os.path.join(REPO_ROOT, "deobf", "deob.py")
LUAU_BIN = os.path.join(REPO_ROOT, "deobf", "bin", "luau")

MAX_SOURCE_BYTES = 2_000_000
MAX_CONCURRENT_JOBS = max(1, int(os.environ.get("MAX_CONCURRENT_JOBS", "1")))
# Last-resort kill switch so one stuck job can't hold a slot forever.
HARD_KILL_SECONDS = int(os.environ.get("HARD_KILL_SECONDS", str(20 * 60)))
JOB_TTL_SECONDS = 60 * 60
MAX_LOG_LINES = 2000

OBFUSCATORS = ("auto-detect", "luraph_v15", "ironbrew1", "generic")

_JOBS = {}
_QUEUE = []
_LOCK = threading.Lock()
_SLOTS = threading.Semaphore(MAX_CONCURRENT_JOBS)

# Log line prefix -> phase; the engine prints these to stderr as it goes.
_PHASES = [
    (re.compile(r"^\[\*\] obfuscator: "), "detect"),
    (re.compile(r"^\[\*\] tracing "), "trace"),
    (re.compile(r"^\[\*\] (devirtualizing|devirt round)"), "devirt"),
    (re.compile(r"^\[\+\] result: "), "finish"),
]
_OBF_LINE = re.compile(r"^\[\*\] obfuscator: (.+?)(?: \(detected, ([0-9.]+)\))?$")
# "script error: ..." / "compile error: ..." = the script died while it ran (aborted/budget stops are normal)
_RUN_STATUS = re.compile(r"^\[\*\] run status: (?:((?:script|compile) error.*)|.*)$")
_FUNCS_LINE = re.compile(r"^\[\*\]\s+(\d+) functions,")   # the lifter's summary: "1867 functions, 0 unlifted blocks, ..."
# First line of every deobfuscated file we return. WATERMARK="" turns it off. The engine's own
# credit header ("Deobfuscated by ccjvwsod ...") stays below it.
WATERMARK = os.environ.get("WATERMARK", "deob by josz").strip()
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
MAX_LINE_CHARS = 2000


# Luarmor V4 wrappers download the real script from Luarmor's servers (with each user's key) and
# kick when run directly, so the tracer only sees a timeout: say what the file is instead.
_LUARMOR = re.compile(r"Luarmor V4 bootstrapper", re.I)
LUARMOR_NOTE = ("Es un cargador (wrapper) de Luarmor V4: baja el script real desde los servidores de Luarmor "
                "con la clave de cada usuario y no funciona ejecutado solo. Este archivo no contiene nada que "
                "deofuscar; hace falta el script que el cargador descarga.")


def engine_available():
    return os.path.isfile(DEOB_PY) and os.path.isfile(LUAU_BIN)


def _read_first(paths, key=None):
    """First readable cgroup file: its `key <n>` line, or its whole value."""
    for path in paths:
        try:
            with open(path) as f:
                if key is None:
                    return f.read().strip()
                for line in f:
                    k, _, v = line.partition(" ")
                    if k == key:
                        return int(v)
        except (OSError, ValueError):
            continue
    return None


def _oom_kills():
    return _read_first(["/sys/fs/cgroup/memory.events", "/sys/fs/cgroup/memory/memory.oom_control"], "oom_kill")


def memory_limit_mb():
    raw = _read_first(["/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"])
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None  # "max" (v2) or unreadable: no limit we can report
    return n // (1024 * 1024) if n < 1 << 50 else None


class Job:
    def __init__(self, filename, obfuscator, no_devirt, timeout, action):
        self.id = uuid.uuid4().hex
        name = _SAFE_NAME.sub("_", os.path.basename(filename or "")).strip("._") or "script.lua"
        self.filename = name[:120]
        self.obfuscator = obfuscator
        self.no_devirt = bool(no_devirt)
        self.timeout = int(timeout)
        self.action = action
        self.status = "queued"  # queued -> running -> done | error | cancelled
        self.phase = "queued"
        self.log = []
        self.output = None
        self.detected_obfuscator = None
        self.confidence = None
        self.url = None            # validated link to download the script from (instead of pasted text)
        self.source_label = None   # host/path of that link, without the query string
        self.loader_note = None    # set when the input is a known remote-script loader
        self.run_error = None      # the script crashed in the sandbox: the result covers only what ran before
        self.functions = None      # functions the lifter rebuilt (devirtualized runs only)
        self.watermark = " ".join(WATERMARK.split()) or None   # first line added to the output
        self.error = None
        self.created_at = time.time()
        self.started_at = None
        self.finished_at = None
        self.cancel_requested = False
        self.proc = None
        self._lock = threading.Lock()

    def append_log(self, line):
        # Failure paths dump raw luau stdout: \0-prefixed protocol markers and
        # 16 KB heartbeat padding. Keep the text, drop the noise.
        line = _CONTROL.sub("", line).rstrip()
        if not line.strip() or line == "HB":
            return
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + " …"
        with self._lock:
            if len(self.log) < MAX_LOG_LINES:
                self.log.append(line)
            elif len(self.log) == MAX_LOG_LINES:
                self.log.append("[…] registro recortado")
        for pat, phase in _PHASES:
            if pat.match(line):
                self.phase = phase
                break
        m = _OBF_LINE.match(line)
        if m:
            self.detected_obfuscator = m.group(1)
            self.confidence = float(m.group(2)) if m.group(2) else None
        m = _RUN_STATUS.match(line)
        if m:   # each traced run reports one: the last decides
            self.run_error = m.group(1)[:300] if m.group(1) else None
        m = _FUNCS_LINE.match(line)
        if m:
            self.functions = int(m.group(1))   # the last one wins: it is the final round's

    def elapsed_ms(self):
        start = self.started_at or self.created_at
        end = self.finished_at or time.time()
        return int((end - start) * 1000)

    def to_dict(self):
        with self._lock:
            log = list(self.log)
        position = None
        if self.status == "queued":
            with _LOCK:
                position = _QUEUE.index(self.id) + 1 if self.id in _QUEUE else None
        return {
            "id": self.id,
            "action": self.action,
            "filename": self.filename,
            "sourceUrl": self.source_label,
            "obfuscator": self.obfuscator,
            "noDevirt": self.no_devirt,
            "status": self.status,
            "phase": self.phase,
            "queuePosition": position,
            "log": log,
            "output": self.output,
            "detectedObfuscator": self.detected_obfuscator,
            "confidence": self.confidence,
            "functions": self.functions,
            "runError": self.run_error,
            "error": self.error,
            "elapsedMs": self.elapsed_ms(),
        }


def _prune():
    cutoff = time.time() - JOB_TTL_SECONDS
    with _LOCK:
        for jid in [j for j, job in _JOBS.items() if job.finished_at and job.finished_at < cutoff]:
            del _JOBS[jid]


def create_job(source, filename, obfuscator, no_devirt, timeout, action, url=None):
    if action not in ("deobfuscate", "detect"):
        raise ValueError("Acción inválida.")
    if obfuscator not in OBFUSCATORS:
        raise ValueError("Ofuscador desconocido.")
    if url:
        try:
            url = fetch.normalize(url)   # rejects bad links now; the download itself runs in the job
        except fetch.FetchError as exc:
            raise ValueError(str(exc)) from exc
        filename, source = fetch.filename_for(url), ""
    elif len(source.encode("utf-8", errors="ignore")) > MAX_SOURCE_BYTES:
        raise ValueError("Script demasiado grande (máx. %d KB)." % (MAX_SOURCE_BYTES // 1000))

    _prune()
    job = Job(filename, obfuscator, no_devirt, timeout, action)
    if url:
        job.url, job.source_label = url, fetch.display(url)
    elif _LUARMOR.search(source[:4000]):
        job.loader_note = LUARMOR_NOTE
    with _LOCK:
        _JOBS[job.id] = job
        _QUEUE.append(job.id)
    threading.Thread(target=_run_job, args=(job, source), daemon=True).start()
    return job


def get_job(job_id):
    with _LOCK:
        return _JOBS.get(job_id)


def _kill_tree(proc):
    """deob.py spawns luau children; killing only deob.py would orphan them at 100% CPU."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def cancel_job(job):
    job.cancel_requested = True
    with _LOCK:
        if job.id in _QUEUE:
            _QUEUE.remove(job.id)
            job.status, job.error = "cancelled", "Cancelado antes de empezar."
            job.phase, job.finished_at = "finish", time.time()
    proc = job.proc
    if proc:
        _kill_tree(proc)


def stats():
    with _LOCK:
        running = sum(1 for j in _JOBS.values() if j.status == "running")
        queued = len(_QUEUE)
    return {"running": running, "queued": queued, "maxConcurrent": MAX_CONCURRENT_JOBS}


def _reader(stream, sink):
    try:
        for line in stream:
            sink(line.rstrip("\n"))
    except Exception:
        pass


def _diagnose(job, code, oom_before):
    """A readable cause for a failed run; the engine's own message can be empty
    when the luau VM is killed from outside (it then only echoes luau's output)."""
    if job.loader_note:
        return job.loader_note
    oom_after = _oom_kills()
    if oom_before is not None and oom_after is not None and oom_after > oom_before:
        limit = memory_limit_mb()
        msg = ("Sin memoria: el sistema mató la VM de Luau%s. Sube la RAM del servicio en Railway, "
               "baja MAX_CONCURRENT_JOBS o prueba el modo Rápido."
               % (" (límite del contenedor: %d MB)" % limit if limit else ""))
        job.append_log("[!] " + msg)
        return msg
    if code == -9:
        return "El trabajo superó el tiempo máximo del servidor."
    if any(line.startswith("[!] timed out after") for line in job.log):
        return ("El script no terminó dentro del entorno simulado: se quedó en un bucle sin fin. Suele ser una "
                "comprobación anti-manipulación de una versión de Luraph que el motor todavía no entiende.")
    if any(line == "[!]" for line in job.log):
        msg = ("La VM de Luau se cerró de golpe sin mensaje de error: casi siempre es falta de memoria "
               "del servidor (o un crash nativo del script).")
        job.append_log("[!] " + msg)
        return msg
    last = next((l for l in reversed(job.log) if l.startswith("[!]") and len(l) > 4), None)
    return last[4:].strip()[:300] if last else "El motor no produjo salida (código %s)." % code


def _run_job(job, source):
    _SLOTS.acquire()
    with _LOCK:
        if job.id in _QUEUE:
            _QUEUE.remove(job.id)
    if job.cancel_requested:
        if job.status != "cancelled":
            job.status, job.error = "cancelled", "Cancelado antes de empezar."
            job.phase, job.finished_at = "finish", time.time()
        _SLOTS.release()
        return

    job.status = "running"
    job.phase = "detect"
    job.started_at = time.time()
    if job.loader_note:
        job.append_log("[!] " + job.loader_note)
    discord.notify_started(job)

    workdir = tempfile.mkdtemp(prefix="deobjob_")
    stdout_lines = []
    try:
        if job.url:
            job.phase = "fetch"
            job.append_log("[*] descargando %s" % job.source_label)
            try:
                data, _ = fetch.download(job.url, MAX_SOURCE_BYTES)
            except fetch.FetchError as exc:
                job.status, job.error = "error", str(exc)
                job.append_log("[!] %s" % exc)
                return
            job.append_log("[*] descargados %d KB" % -(-len(data) // 1000))
            job.phase = "detect"
            if _LUARMOR.search(data[:4000].decode("latin-1")):
                job.loader_note = LUARMOR_NOTE
                job.append_log("[!] " + LUARMOR_NOTE)
        else:
            # the engine reads the file as bytes: a pasted (Unicode) script goes in as UTF-8, like a real file
            data = source.encode("utf-8", errors="replace")
        in_path = os.path.join(workdir, job.filename)
        with open(in_path, "wb") as f:
            f.write(data)

        out_path = os.path.join(workdir, "out.lua")
        cmd = [sys.executable, DEOB_PY, in_path, "--timeout", str(job.timeout)]
        if job.action == "detect":
            cmd.append("--detect")
        else:
            cmd += ["-o", out_path]
            if job.no_devirt:
                cmd.append("--no-devirt")
        if job.obfuscator != "auto-detect":
            cmd += ["--obfuscator", job.obfuscator]

        oom_before = _oom_kills()
        job.proc = subprocess.Popen(
            cmd,
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,  # own process group, so _kill_tree gets the luau children too
        )
        if job.cancel_requested:  # cancelled between dequeue and spawn
            _kill_tree(job.proc)

        def on_stdout(line):
            stdout_lines.append(line)
            job.append_log(line)

        readers = [
            threading.Thread(target=_reader, args=(job.proc.stdout, on_stdout), daemon=True),
            threading.Thread(target=_reader, args=(job.proc.stderr, job.append_log), daemon=True),
        ]
        for t in readers:
            t.start()

        try:
            code = job.proc.wait(timeout=HARD_KILL_SECONDS)
        except subprocess.TimeoutExpired:
            _kill_tree(job.proc)
            job.proc.wait()
            code = -9
            job.append_log("[!] límite del servidor excedido (%d s): proceso terminado" % HARD_KILL_SECONDS)
        for t in readers:
            t.join(timeout=5)

        if job.cancel_requested:
            job.status, job.error = "cancelled", "Cancelado por el usuario."
        elif job.action == "detect":
            parts = stdout_lines[-1].split("\t") if stdout_lines else []
            if code == 0 and len(parts) >= 3:
                job.detected_obfuscator = parts[2]
                job.confidence = None if parts[1] == "forced" else float(parts[1])
                job.status = "done"
            else:
                job.status, job.error = "error", "La detección falló (código %s)." % code
        elif code == 0 and os.path.exists(out_path):
            with open(out_path, "r", encoding="utf-8", errors="replace") as f:
                job.output = f.read()
            if job.watermark:
                job.output = "-- %s\n%s" % (job.watermark, job.output)
            job.status = "done"
        else:
            job.status = "error"
            job.error = _diagnose(job, code, oom_before)
    except Exception as exc:  # noqa: BLE001
        job.status, job.error = "error", str(exc)
        job.append_log("[!] %s" % exc)
    finally:
        if job.proc:
            _kill_tree(job.proc)  # stragglers (e.g. a long-lived luau REPL) die with the job
        job.phase = "finish"
        job.finished_at = time.time()
        job.proc = None
        shutil.rmtree(workdir, ignore_errors=True)
        _SLOTS.release()
        discord.notify_finished(job)
