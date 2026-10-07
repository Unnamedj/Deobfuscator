"""
Luau Deobfuscator — FastAPI backend.

Serves the static frontend and a small job API in front of deobf/deob.py.
Run with: uvicorn server.app:app --host 0.0.0.0 --port $PORT
"""

import os


def _load_dotenv(path):
    """Minimal .env loader for local runs; real env vars (Railway) win."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import hashlib  # noqa: E402
from typing import Optional  # noqa: E402

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import HTMLResponse, PlainTextResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from . import discord, jobs  # noqa: E402

app = FastAPI(title="Luau Deobfuscator", docs_url=None, redoc_url=None)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _asset_version():
    h = hashlib.sha1()
    for root, _, files in sorted(os.walk(STATIC_DIR)):
        for name in sorted(files):
            with open(os.path.join(root, name), "rb") as f:
                h.update(name.encode() + f.read())
    return h.hexdigest()[:10]


# index.html references every asset as ?v=__V__: a deploy changes the URLs, so
# browsers (iOS Safari especially) can never pair new HTML with stale CSS/JS.
ASSET_VERSION = _asset_version()
with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as _f:
    INDEX_HTML = _f.read().replace("__V__", ASSET_VERSION)


@app.middleware("http")
async def cache_headers(request: Request, call_next):
    response = await call_next(request)
    if "cache-control" not in response.headers:
        if request.query_params.get("v") == ASSET_VERSION:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
@app.get("/index.html", response_class=HTMLResponse, include_in_schema=False)
def index():
    return HTMLResponse(INDEX_HTML, headers={"Cache-Control": "no-cache"})
SAMPLES_DIR = os.path.join(jobs.REPO_ROOT, "samples")
SAMPLE_LABELS = {"-obfuscated.lua": "Luraph v15", "-ib1.lua": "IronBrew 1"}


class JobRequest(BaseModel):
    action: str = "deobfuscate"
    source: str = ""                 # the script text, or...
    url: Optional[str] = None        # ...a link the server downloads it from (wins over `source`)
    filename: str = "script.lua"
    obfuscator: str = "auto-detect"
    noDevirt: bool = False
    timeout: int = Field(default=90, ge=10, le=600)


def _samples():
    if not os.path.isdir(SAMPLES_DIR):
        return {}
    out = {}
    for name in sorted(os.listdir(SAMPLES_DIR)):
        for suffix, label in SAMPLE_LABELS.items():
            if name.endswith(suffix):
                out[name] = label
    return out


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "engineAvailable": jobs.engine_available(),
        "discord": discord.configured(),
        "memoryLimitMb": jobs.memory_limit_mb(),
        **jobs.stats(),
    }


@app.get("/api/samples")
def list_samples():
    return [{"name": n, "label": l} for n, l in _samples().items()]


@app.get("/api/samples/{name}", response_class=PlainTextResponse)
def get_sample(name: str):
    if name not in _samples():
        raise HTTPException(404, "Ejemplo no encontrado.")
    with open(os.path.join(SAMPLES_DIR, name), encoding="latin-1") as f:
        return f.read()


@app.post("/api/jobs")
def create_job(req: JobRequest):
    if not req.url and not req.source.strip():
        raise HTTPException(400, "Pega el script o un enlace a él.")
    if not jobs.engine_available():
        raise HTTPException(503, "El motor no está disponible en el servidor.")
    try:
        job = jobs.create_job(
            req.source, req.filename, req.obfuscator, req.noDevirt, req.timeout, req.action, req.url
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": job.id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "Trabajo no encontrado (puede haber expirado).")
    return job.to_dict()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "Trabajo no encontrado.")
    jobs.cancel_job(job)
    return {"ok": True}


# Static frontend last: explicit /api/* routes above always win.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
