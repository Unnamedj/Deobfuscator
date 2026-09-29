"""Discord webhook notifications: job started / finished (with the result file) / failed."""

import json
import os
import queue
import sys
import threading
import urllib.error
import urllib.request
import uuid

MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
COLORS = {"start": 0x5865F2, "done": 0x57F287, "warn": 0xFEE75C, "error": 0xED4245, "cancelled": 0x8A92A6}
PREVIEW_LINES = 6
PREVIEW_MAX_CHARS = 900


# Test webhook; the DISCORD_WEBHOOK_URL env var overrides it.
DEFAULT_WEBHOOK_URL = (
    "https://discord.com/api/webhooks/1554271982919356507/"
    "K8-so0dK9aQbtxM6TdcyJ_TQqmIwQw1jL_ZBvfwhqAIKQsuQwGAwehmuRqMaWGJL0oum"
)


def _url():
    return os.environ.get("DISCORD_WEBHOOK_URL", "").strip() or DEFAULT_WEBHOOK_URL


def configured():
    return bool(_url())


def _post(payload, attachment=None):
    url = _url()
    if not url:
        return
    if attachment:
        name, data = attachment
        boundary = uuid.uuid4().hex
        body = b"".join([
            b"--%s\r\n" % boundary.encode(),
            b'Content-Disposition: form-data; name="payload_json"\r\n',
            b"Content-Type: application/json\r\n\r\n",
            json.dumps(payload).encode("utf-8"),
            b"\r\n--%s\r\n" % boundary.encode(),
            b'Content-Disposition: form-data; name="files[0]"; filename="%s"\r\n' % name.encode("utf-8"),
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n",
            data,
            b"\r\n--%s--\r\n" % boundary.encode(),
        ])
        ctype = "multipart/form-data; boundary=%s" % boundary
    else:
        body = json.dumps(payload).encode("utf-8")
        ctype = "application/json"
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": ctype, "User-Agent": "LuauDeobfuscator (web, 1.0)"},
    )
    try:
        urllib.request.urlopen(req, timeout=20).close()
    except urllib.error.HTTPError as exc:  # noqa: BLE001 - a notification must never break a job
        print("[discord] webhook failed: HTTP %s %s" % (exc.code, exc.read()[:300]), file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print("[discord] webhook failed: %s" % exc, file=sys.stderr)


_OUTBOX = queue.Queue()


def _worker():
    while True:
        payload, attachment = _OUTBOX.get()
        _post(payload, attachment)


threading.Thread(target=_worker, daemon=True).start()


def _send(payload, attachment=None):
    """Queued, one sender thread: messages keep their order and never block a job."""
    if _url():
        _OUTBOX.put((payload, attachment))


# ---- message building -------------------------------------------------------

def _kv(*pairs):
    """`**Key** · value` lines, skipping empty values."""
    return "\n".join("**%s** · %s" % (k, v) for k, v in pairs if v not in (None, ""))


def _embed(title, color, description, footer):
    return {"title": title, "color": color, "description": description[:4000], "footer": {"text": footer}}


def _message(*embeds):
    return {"username": "Luau Deobfuscator", "embeds": list(embeds)}


def _footer(job):
    return "Luau Deobfuscator" + (" · %s" % job.watermark if job.watermark else "")


def _secs(ms):
    return "%.1fs" % (ms / 1000)


def _obfuscator(job):
    name = job.detected_obfuscator or ("Auto-detect" if job.obfuscator == "auto-detect" else job.obfuscator)
    if job.confidence is not None:
        name += " (%d%%)" % round(job.confidence * 100)
    return name


def _mode(job):
    return "Fast · behaviour trace" if job.no_devirt else "Full · devirtualization"


def _is_trace(output):
    return "(dynamic trace)" in (output or "")[:600]


def _preview(output):
    """First code lines of the result: the leading comment header (credits, notes) is skipped."""
    lines = (output or "").split("\n")
    i = 0
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("--")):
        i += 1
    shown = [l.replace("\t", "    ")[:90].rstrip() for l in lines[i:i + PREVIEW_LINES]]
    text = "\n".join(shown).replace("```", "`​``")[:PREVIEW_MAX_CHARS]
    return "```lua\n%s\n```" % text if text.strip() else ""


def notify_started(job):
    if job.action == "detect":
        desc = _kv(("Input", "`%s`" % job.filename), ("Obfuscator", _obfuscator(job)))
        _send(_message(_embed("🔍 Detection started", COLORS["start"], desc, _footer(job))))
        return
    desc = _kv(("Input", "`%s`" % job.filename), ("Obfuscator", _obfuscator(job)), ("Mode", _mode(job)))
    _send(_message(_embed("⚙️ Deobfuscation started", COLORS["start"], desc, _footer(job))))


def notify_finished(job):
    elapsed = _secs(job.elapsed_ms())

    if job.status == "cancelled":
        desc = _kv(("Input", "`%s`" % job.filename), ("Time", elapsed))
        _send(_message(_embed("⏹️ Job cancelled", COLORS["cancelled"], desc, _footer(job))))
        return

    if job.status != "done":
        tail = "\n".join(job.log[-8:])[-1200:].replace("```", "`​``")
        desc = _kv(("Input", "`%s`" % job.filename), ("Obfuscator", _obfuscator(job)), ("Time", elapsed))
        desc += "\n\n**Reason**\n%s" % (job.error or "Unknown error")
        if tail:
            desc += "\n```\n%s\n```" % tail
        _send(_message(_embed("❌ Deobfuscation failed", COLORS["error"], desc, _footer(job))))
        return

    if job.action == "detect":
        desc = _kv(("Input", "`%s`" % job.filename), ("Obfuscator", _obfuscator(job)), ("Time", elapsed))
        _send(_message(_embed("✅ Detection complete", COLORS["done"], desc, _footer(job))))
        return

    output = job.output or ""
    data = output.encode("utf-8")
    fits = len(data) <= MAX_ATTACHMENT_BYTES
    warn = job.run_error
    desc = "Your cleaned script is attached below." if fits else \
        "The result is %d KB, too big to attach here: download it from the web page." % (len(data) // 1024)
    if warn:
        desc = ("The script crashed while running in the sandbox, so the result only covers what ran before "
                "that. " + desc)
    desc += "\n" + _kv(
        ("Input", "`%s`" % job.filename),
        ("Obfuscator", _obfuscator(job)),
        ("Mode", _mode(job)),
        ("Warning", "`%s`" % warn[:200].replace("`", "'") if warn else None),
        ("Watermark", "🏷️ %s" % job.watermark if job.watermark else None),
        ("Functions", "{:,}".format(job.functions) if job.functions is not None else None),
        ("Lines", "{:,}".format(output.count("\n") + 1)),
        ("Time", elapsed),
    )
    preview = _preview(output)
    if preview:
        desc += "\n\n**Preview (first %d lines):**\n%s" % (PREVIEW_LINES, preview)
    status = "Behaviour trace (not devirtualized)" if _is_trace(output) else "Deobfuscated & devirtualized"
    detection = _embed(
        "✅ Detection complete", COLORS["done"],
        _kv(("Obfuscator", _obfuscator(job)), ("Status", status)), _footer(job))
    main = _embed("⚠️ Deobfuscation finished with warnings" if warn else "✅ Deobfuscation complete",
                  COLORS["warn"] if warn else COLORS["done"], desc, _footer(job))
    message = _message(main, detection)
    base = job.filename.rsplit(".", 1)[0] or "output"
    _send(message, attachment=("%s_deobf.lua" % base, data) if fits else None)
