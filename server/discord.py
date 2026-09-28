"""Discord webhook notifications for job start/finish/failure."""

import json
import os
import urllib.request

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()


def _send(title, description, color, fields=None):
    if not DISCORD_WEBHOOK_URL:
        return
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "fields": [
            {"name": k, "value": str(v)[:1024], "inline": True} for k, v in (fields or {}).items()
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


def notify_started(job):
    _send(
        "🔄 Trabajo iniciado" if job.action == "deobfuscate" else "🔍 Detección iniciada",
        f"Archivo `{job.filename}`",
        0x5865F2,
        {
            "Ofuscador": job.obfuscator,
            "Modo": "rápido (trace)" if job.no_devirt else "completo (devirt)",
        },
    )


def notify_finished(job):
    elapsed_ms = job.elapsed_ms()
    if job.status == "done":
        _send(
            "✅ Trabajo completado",
            f"Archivo `{job.filename}`",
            0x3DDC97,
            {
                "Ofuscador detectado": job.detected_obfuscator or "-",
                "Tiempo": f"{elapsed_ms} ms",
            },
        )
    else:
        _send(
            "❌ Trabajo fallido",
            f"Archivo `{job.filename}`",
            0xEF5C66,
            {
                "Error": (job.error or "ver registro")[:200],
                "Tiempo": f"{elapsed_ms} ms",
            },
        )
