"""Discord webhook notifications: job started / finished (with the result file) / failed."""

import json
import os
import queue
import sys
import threading
import urllib.request
import uuid

MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024
COLORS = {"start": 0x7C5CFF, "done": 0x2FD39A, "error": 0xF0566A, "cancelled": 0x8A92A6}
MODE = {True: "Rápido (solo trace)", False: "Completo (devirtualización)"}


def _url():
    return os.environ.get("DISCORD_WEBHOOK_URL", "").strip()


def _fmt_ms(ms):
    s = ms / 1000
    return "%.1f s" % s if s < 60 else "%d min %02d s" % (s // 60, s % 60)


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
        urllib.request.urlopen(req, timeout=15).close()
    except Exception as exc:  # noqa: BLE001 - a notification must never break a job
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


def _embed(title, color, fields, description=None):
    embed = {
        "title": title,
        "color": color,
        "fields": [{"name": k, "value": str(v)[:1024], "inline": True} for k, v in fields.items()],
        "footer": {"text": "Luau Deobfuscator"},
    }
    if description:
        embed["description"] = description[:4000]
    return {"username": "Luau Deobfuscator", "embeds": [embed]}


def notify_started(job):
    title = "🔍 Detección iniciada" if job.action == "detect" else "⚙️ Deofuscación iniciada"
    fields = {"Archivo": "`%s`" % job.filename, "Ofuscador": job.obfuscator}
    if job.action == "deobfuscate":
        fields["Modo"] = MODE[job.no_devirt]
    _send(_embed(title, COLORS["start"], fields))


def notify_finished(job):
    elapsed = _fmt_ms(job.elapsed_ms())
    detected = job.detected_obfuscator or "—"
    if job.confidence is not None:
        detected += " (%d%%)" % round(job.confidence * 100)

    if job.status == "cancelled":
        _send(_embed("⏹️ Trabajo cancelado", COLORS["cancelled"],
                     {"Archivo": "`%s`" % job.filename, "Tiempo": elapsed}))
        return
    if job.status != "done":
        tail = "\n".join(job.log[-8:])
        _send(_embed("❌ Trabajo fallido", COLORS["error"],
                     {"Archivo": "`%s`" % job.filename, "Ofuscador": detected, "Tiempo": elapsed},
                     description="**%s**\n```\n%s\n```" % (job.error or "Error", tail[-1500:])))
        return
    if job.action == "detect":
        _send(_embed("✅ Detección completada", COLORS["done"],
                     {"Archivo": "`%s`" % job.filename, "Ofuscador": detected, "Tiempo": elapsed}))
        return

    data = (job.output or "").encode("utf-8")
    fields = {
        "Archivo": "`%s`" % job.filename,
        "Ofuscador": detected,
        "Modo": MODE[job.no_devirt],
        "Tiempo": elapsed,
        "Líneas": "{:,}".format((job.output or "").count("\n") + 1),
    }
    base = job.filename.rsplit(".", 1)[0] or "output"
    if len(data) <= MAX_ATTACHMENT_BYTES:
        _send(_embed("✅ Deofuscación completada", COLORS["done"], fields),
              attachment=("%s.deobf.luau" % base, data))
    else:
        _send(_embed("✅ Deofuscación completada", COLORS["done"], fields,
                     description="La salida pesa %d KB, demasiado para adjuntarla; descárgala desde la web."
                                 % (len(data) // 1024)))
