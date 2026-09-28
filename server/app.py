"""
Luau Deobfuscator — FastAPI backend.

Serves the static frontend and a small job API in front of deobf/deob.py.
Run with: uvicorn server.app:app --host 0.0.0.0 --port $PORT
"""

import os

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import jobs

app = FastAPI(title="Luau Deobfuscator")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class JobRequest(BaseModel):
    action: str = "deobfuscate"
    source: str
    filename: str = "script.lua"
    obfuscator: str = "auto-detect"
    noDevirt: bool = False
    timeout: int = Field(default=90, ge=10, le=900)


@app.get("/api/health")
def health():
    return {"ok": True, "engineAvailable": jobs.engine_available()}


@app.post("/api/jobs")
def create_job(req: JobRequest):
    if not req.source.strip():
        raise HTTPException(400, "Script vacío.")
    try:
        job = jobs.create_job(
            req.source, req.filename, req.obfuscator, req.noDevirt, req.timeout, req.action
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"id": job.id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job no encontrado.")
    return job.to_dict()


# Static frontend last: explicit /api/* routes above always win.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
