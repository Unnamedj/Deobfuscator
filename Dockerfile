# ---- Stage 1: compile luau + luau-ast from source ----------------------
# The engine (deobf/deob.py) shells out to a native Luau interpreter it
# runs the protected script against. No prebuilt binary ships in the repo;
# CLAUDE.md requires a locally built luau (Vector3 metatable left
# writable) rather than a stock download. --portable avoids -march=native
# so the binary works on whatever CPU the host (Railway) runs it on.
FROM debian:bookworm-slim AS luau-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        git cmake ninja-build g++ gcc ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY deobf/build_luau.py deobf/build_luau.py
COPY docker/build_luau_ast.py docker/build_luau_ast.py

RUN python3 deobf/build_luau.py --portable
RUN python3 docker/build_luau_ast.py --portable

# ---- Stage 2: runtime ----------------------------------------------------
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt

COPY deobf/ deobf/
COPY server/ server/
COPY samples/ samples/
COPY --from=luau-builder /build/deobf/bin/luau deobf/bin/luau
COPY --from=luau-builder /build/deobf/bin/luau-ast deobf/bin/luau-ast
RUN chmod +x deobf/bin/luau deobf/bin/luau-ast

ENV PORT=8000 PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT}"]
