# Luau Deobfuscator — Web (Railway)

Interfaz web para `deobf/deob.py`: un deofuscador **dinámico** de scripts
Roblox Luau. El script protegido corre en una VM real de Luau contra un
entorno simulado de Roblox (`deobf/envlog.luau`), se traza su
comportamiento y, para Luraph v15 / IronBrew 1, se devirtualiza el bytecode
de vuelta a Luau legible con su control de flujo real.

## Por qué un servidor Docker (y no serverless)

El motor necesita un **binario `luau` nativo** compilado desde el código
fuente con un patch propio, corre como **un subproceso por trabajo**
(mantiene estado global, ver `CLAUDE.md`) y un script grande puede tardar
**minutos**. Eso descarta funciones serverless; Railway lo corre como un
servicio Docker de larga duración.

## Estructura

```
Dockerfile                multi-stage: compila luau/luau-ast desde fuente y arma la imagen
docker/build_luau_ast.py  compila deobf/bin/luau-ast (deobf/build_luau.py compila luau)
railway.json              config de Railway (builder Dockerfile, healthcheck, reinicios)
deobf/                    el motor, tal cual se recibió
server/
  app.py                  FastAPI: API de trabajos + sirve el frontend
  jobs.py                 cola de trabajos: deob.py como subproceso, fases, cancelación
  discord.py              webhook: inicio / fin (con el .luau adjunto) / error
  static/                 frontend (HTML/CSS/JS, sin build; highlight.js incluido en vendor/)
samples/                  scripts de ejemplo (la web los ofrece en "Ejemplos…")
```

### API

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/api/health` | Motor disponible, Discord configurado, trabajos en curso/en cola |
| `POST` | `/api/jobs` | Crea un trabajo (`action`: `deobfuscate` \| `detect`) y responde al toque con su `id` |
| `GET` | `/api/jobs/{id}` | Estado, fase (`queued` → `detect` → `trace` → `devirt` → `finish`), registro y resultado |
| `POST` | `/api/jobs/{id}/cancel` | Cancela (mata `deob.py` y todos sus procesos `luau`) |
| `GET` | `/api/samples`, `/api/samples/{name}` | Ejemplos incluidos |

Los trabajos viven en memoria una hora después de terminar. La web guarda el
trabajo en curso en el navegador, así que recargar la página lo retoma.

## Deploy en Railway

1. [railway.com](https://railway.com) → **New Project → Deploy from GitHub
   repo** → elige este repo y la rama. Railway detecta `railway.json` y usa
   el `Dockerfile`.
2. En el servicio → **Variables**, agrega:
   - `DISCORD_WEBHOOK_URL` = la URL de tu webhook
   - (opcional) `MAX_CONCURRENT_JOBS`, `HARD_KILL_SECONDS` — ver `.env.example`
3. **Settings → Networking → Generate Domain** para tener la URL pública.
4. El primer build compila Luau desde fuente (varios minutos). Cuando el
   healthcheck (`/api/health`) responde, la web está en línea.

Railway inyecta `PORT` solo; no hay que configurarlo.

## Desarrollo local

Necesitas Python 3.11+, git, cmake y g++:

```bash
python3 deobf/build_luau.py --portable        # -> deobf/bin/luau
python3 docker/build_luau_ast.py --portable   # -> deobf/bin/luau-ast
pip install -r server/requirements.txt
cp .env.example .env                          # pon tu DISCORD_WEBHOOK_URL
uvicorn server.app:app --reload --port 8000   # http://localhost:8000
```

## Límites conocidos

- Detecta `luraph_v15` e `ironbrew1`; el resto cae a `generic` (solo trace,
  sin devirtualización).
- El trace no incluye ramas que no se ejecutaron (la salida devirtualizada sí).
- Discord no admite adjuntos de más de 8 MB: en ese caso el mensaje avisa
  y el archivo se descarga desde la web.

## Uso previsto

Herramienta educativa y de investigación: entender técnicas de ofuscación
en scripts Lua/Luau. Analiza solo código que tengas permiso de analizar.
