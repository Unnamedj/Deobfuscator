# Luau Deobfuscator — Web (Render)

Interfaz web para `deobf/deob.py`: un deobfuscador **dinámico** de scripts
Roblox Luau. El script protegido corre en una VM real de Luau contra un
entorno simulado de Roblox (`deobf/envlog.luau`), se traza su
comportamiento y, para Luraph v15 / IronBrew1, se devirtualiza el bytecode
de vuelta a Luau legible con control de flujo real.

## Por qué Render y no Vercel

El motor necesita:

- un **binario nativo `luau`** (compilado desde el código fuente de Luau,
  con un patch específico — no un build estándar descargado),
- correr como **subproceso independiente por cada trabajo** (mantiene
  estado global entre corridas, ver `CLAUDE.md`),
- y puede tardar **de segundos a 15+ minutos** en scripts grandes con
  devirtualización completa.

Nada de eso cabe en una función serverless de Vercel (límite duro de
minutos, sin binarios nativos persistentes). Render corre esto como un
**Web Service Docker** de larga duración, sin esas limitaciones.

## Arquitectura

```
Dockerfile              build multi-stage: compila luau/luau-ast, arma la imagen final
docker/build_luau_ast.py  compila deobf/bin/luau-ast (deob.py usa build_luau.py para `luau`)
deobf/                   el motor real (tal cual se recibió; deobf/bin/*.exe se compilan en el build)
server/
  app.py                 FastAPI: /api/health, /api/jobs (POST), /api/jobs/{id} (GET), sirve el frontend
  jobs.py                job runner asíncrono: corre deob.py como subprocess en un hilo, con timeout duro
  discord.py              notificaciones de inicio/fin/error a un webhook de Discord
  static/                 frontend (HTML/CSS/JS vanilla, sin build step)
render.yaml              Blueprint de Render (Web Service, runtime docker)
samples/                  scripts de prueba (con y sin ofuscar) que trae el propio proyecto
```

**Flujo de un trabajo:** `POST /api/jobs` crea el job y responde al toque
con un `id` (no bloquea la request); un hilo en background corre
`python deobf/deob.py <script> ...` como subproceso; el frontend hace
`GET /api/jobs/{id}` cada 1.5s mostrando log y estado en vivo hasta que
termina. Discord recibe un webhook al iniciar y al terminar (éxito o
error).

## Desarrollo local

Necesitas Python 3.11+ y, para deofuscar de verdad (no solo servir la UI),
el binario `luau` compilado:

```bash
python3 deobf/build_luau.py --portable   # compila deobf/bin/luau (necesita git, cmake, g++)
python3 docker/build_luau_ast.py --portable   # compila deobf/bin/luau-ast (opcional, solo para checks)

pip install -r server/requirements.txt
uvicorn server.app:app --reload --port 8000
# abre http://localhost:8000
```

Sin `deobf/bin/luau`, la UI carga igual pero el banner de "motor no
disponible" se muestra y cualquier trabajo falla.

## Variables de entorno

| Variable              | Descripción                                                        |
| ---------------------- | ------------------------------------------------------------------- |
| `DISCORD_WEBHOOK_URL`  | Webhook de Discord. Notifica inicio, fin (éxito) y fin (error) de cada trabajo. Vacío = sin notificaciones. |
| `PORT`                 | Puerto donde escucha uvicorn (Render lo inyecta solo). Default 8000 local. |

Copia `.env.example` a `.env` para desarrollo local si usas un cargador de
env vars, o expórtalas directo en la shell.

## Deploy en Render

1. Conecta el repo en [render.com](https://render.com) → **New → Blueprint**
   y apunta a este repo (usa `render.yaml`), o crea un **Web Service**
   manual con **Runtime: Docker** y este `Dockerfile`.
2. En **Environment**, agrega `DISCORD_WEBHOOK_URL` con la URL de tu
   webhook de Discord (Server Settings → Integrations → Webhooks →
   New Webhook → Copy URL).
3. Deploy. El build compila `luau`/`luau-ast` desde código fuente (tarda
   varios minutos la primera vez); el health check pega a `/api/health`.

**Plan free de Render:** el servicio se duerme tras inactividad y tarda
unos segundos en despertar en la siguiente request — normal, no es un
error. Para scripts grandes con devirtualización completa (varios
minutos), considera un plan pago para evitar que el servicio se duerma
a mitad de un trabajo largo.

## Límites conocidos

- El detector de ofuscadores solo reconoce `luraph_v15` e `ironbrew1`;
  todo lo demás cae al modo `generic` (solo trace de comportamiento, sin
  devirtualización).
- El trace no captura ramas no ejecutadas durante la corrida real.
- Un kill switch del servidor mata cualquier trabajo que exceda 20
  minutos (`server/jobs.py`, `HARD_KILL_SECONDS`), para que un job
  colgado no tumbe el servicio.

## Uso previsto

Herramienta educativa / de investigación para el ámbito estudiantil:
entender técnicas de ofuscación en scripts Lua/Luau. No está pensada para
eludir protecciones en sistemas que no te pertenecen ni tienes
autorización para analizar.
