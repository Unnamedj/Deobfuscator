"""
Vercel Python serverless function backing the web UI.

POST /api/deobfuscate  -> runs a job (deobfuscate | detect)
GET  /api/deobfuscate   -> health check, tells the UI whether the real
                            engine (deobf/deob.py) is wired in yet.

Plugging in the real engine
----------------------------
Replace `deobf/deob.py` (at the project root, alongside this `api/`
folder) with your real deob.py + any supporting modules. As soon as
`deobf.deob.run(...)` stops raising NotImplementedError, this file
automatically uses it — no changes needed here.

Discord notifications
----------------------
Set the DISCORD_WEBHOOK_URL environment variable (Vercel dashboard ->
Project -> Settings -> Environment Variables). Every job posts a
"started", "finished" or "failed" message. If the variable is unset,
notifications are silently skipped.
"""

import json
import os
import re
import time
import traceback
import urllib.request
from http.server import BaseHTTPRequestHandler

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

OBFUSCATOR_SIGNATURES = {
    "luraph_v15": [r"LPH!", r"luraph", r"L0_\d"],
    "ironbrew1": [r"IronBrew", r"IB_", r"__IRONBRW"],
}


# ---------------------------------------------------------------------------
# Discord webhook helpers
# ---------------------------------------------------------------------------

def notify_discord(title, description, color, fields=None):
    if not DISCORD_WEBHOOK_URL:
        return
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "fields": [
            {"name": k, "value": str(v), "inline": True} for k, v in (fields or {}).items()
        ],
        "footer": {"text": "Luau Deobfuscator"},
    }
    payload = json.dumps({"embeds": [embed]}).encode("utf-8")
    try:
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        # A notification failure must never break the actual job.
        pass


# ---------------------------------------------------------------------------
# Engine: real (if present) with a labeled demo fallback
# ---------------------------------------------------------------------------

def engine_available():
    try:
        from deobf import deob  # noqa: F401
    except ImportError:
        return False
    try:
        deob.run(source="", obfuscator="generic", no_devirt=True, timeout=1)
    except NotImplementedError:
        return False
    except Exception:
        # Any other failure means the real engine ran and threw on empty
        # input, which counts as "present".
        return True
    return True


def run_real_engine(source, obfuscator, no_devirt, timeout):
    from deobf import deob
    return deob.run(
        source=source, obfuscator=obfuscator, no_devirt=no_devirt, timeout=timeout
    )


def detect_obfuscator(source):
    for name, patterns in OBFUSCATOR_SIGNATURES.items():
        for pat in patterns:
            if re.search(pat, source):
                return name
    return "generic"


def stub_transform(source, obfuscator, no_devirt):
    """
    Demo-only pass used until deobf/deob.py is wired in. Does a few honest,
    non-destructive cleanups (decodes simple escaped strings, re-indents
    block keywords) so the pipeline is visibly exercised end-to-end, but
    this is NOT real deobfuscation.
    """
    log = []
    detected = obfuscator if obfuscator != "auto-detect" else detect_obfuscator(source)
    log.append(f"[demo] ofuscador detectado: {detected}")
    log.append("[demo] motor real no conectado — aplicando limpieza de muestra")

    text = source

    def hex_repl(m):
        try:
            return chr(int(m.group(1), 16))
        except ValueError:
            return m.group(0)

    def dec_repl(m):
        try:
            n = int(m.group(1))
            return chr(n) if 32 <= n < 127 else m.group(0)
        except ValueError:
            return m.group(0)

    text = re.sub(r"\\x([0-9A-Fa-f]{2})", hex_repl, text)
    text = re.sub(r"\\(\d{1,3})", dec_repl, text)
    log.append("[demo] escapes hex/decimales simples decodificados")

    if not no_devirt:
        lines = [l.rstrip() for l in text.splitlines()]
        indent = 0
        out_lines = []
        dedent_kw = re.compile(r"^\s*(end|else|elseif|until)\b")
        indent_kw = re.compile(
            r"\b(function|do|then|repeat)\s*$|^\s*(if|for|while)\b"
        )
        for line in lines:
            stripped = line.strip()
            if dedent_kw.match(stripped):
                indent = max(indent - 1, 0)
            out_lines.append(("    " * indent) + stripped)
            if indent_kw.search(stripped) and not stripped.startswith("--"):
                indent += 1
        text = "\n".join(out_lines)
        log.append("[demo] reindentado por bloques (function/if/for/while/do)")
    else:
        log.append("[demo] modo rápido: se omitió el reindentado")

    header = (
        "-- ==========================================================\n"
        "-- SALIDA DE DEMOSTRACIÓN — motor real no conectado.\n"
        "-- Conecta deobf/deob.py para una deofuscación real.\n"
        "-- ==========================================================\n"
    )
    return {"output": header + text, "log": log, "detected_obfuscator": detected}


# ---------------------------------------------------------------------------
# Request handling
# ---------------------------------------------------------------------------

def process(data):
    action = data.get("action", "deobfuscate")
    source = data.get("source", "")
    filename = data.get("filename", "script.lua")
    obfuscator = data.get("obfuscator", "auto-detect")
    no_devirt = bool(data.get("noDevirt", False))
    timeout = int(data.get("timeout", 90))

    if not source.strip():
        return {"ok": False, "action": action, "error": "Script vacío.", "log": []}, 400

    notify_discord(
        "🔄 Deobfuscación iniciada" if action == "deobfuscate" else "🔍 Detección iniciada",
        f"Archivo `{filename}`",
        0x5865F2,
        {"Ofuscador": obfuscator, "Modo": "rápido" if no_devirt else "completo"},
    )

    start = time.time()
    try:
        engine = "stub"
        real = engine_available()
        if real:
            engine = "real"
            result = run_real_engine(source, obfuscator, no_devirt, timeout)
        else:
            result = stub_transform(source, obfuscator, no_devirt)

        elapsed_ms = int((time.time() - start) * 1000)

        if action == "detect":
            payload = {
                "ok": True,
                "action": "detect",
                "output": "",
                "log": result["log"],
                "detectedObfuscator": result["detected_obfuscator"],
                "engine": engine,
                "elapsedMs": elapsed_ms,
            }
        else:
            payload = {
                "ok": True,
                "action": "deobfuscate",
                "output": result["output"],
                "log": result["log"],
                "detectedObfuscator": result["detected_obfuscator"],
                "engine": engine,
                "elapsedMs": elapsed_ms,
            }

        notify_discord(
            "✅ Trabajo completado",
            f"Archivo `{filename}`",
            0x3DDC97,
            {
                "Ofuscador detectado": result["detected_obfuscator"],
                "Motor": engine,
                "Tiempo": f"{elapsed_ms} ms",
            },
        )
        return payload, 200

    except Exception as exc:  # noqa: BLE001
        elapsed_ms = int((time.time() - start) * 1000)
        err_text = f"{exc}"
        notify_discord(
            "❌ Trabajo fallido",
            f"Archivo `{filename}`",
            0xEF5C66,
            {"Error": err_text[:200], "Tiempo": f"{elapsed_ms} ms"},
        )
        return {
            "ok": False,
            "action": action,
            "error": err_text,
            "log": [traceback.format_exc()],
        }, 500


class handler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send_json({"engineAvailable": engine_available()})

    def do_POST(self):
        try:
            length = int(self.headers.get("content-length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send_json({"ok": False, "error": "JSON inválido."}, 400)
            return

        payload, status = process(data)
        self._send_json(payload, status)
