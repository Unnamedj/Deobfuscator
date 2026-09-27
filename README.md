# Luau Deobfuscator — Web

Versión web (Next.js + función serverless en Python) del deobfuscador de
escritorio (`deobf_gui.pyw`), pensada para desplegarse en Vercel con
notificaciones de estado a Discord.

## Estructura

```
app/                    UI en Next.js (React + Tailwind)
api/deobfuscate.py      Función serverless (Python) que ejecuta el job y notifica a Discord
deobf/deob.py           Punto de conexión del motor real de deofuscación (placeholder)
vercel.json             Config de Vercel (duración máx. de la función)
```

## ⚠️ Falta conectar el motor real

Lo que me compartiste (`deobf_gui.pyw`) es solo la GUI de escritorio: llama
a `deobf/deob.py` como subproceso, pero ese archivo (con el paquete
`deobf/` completo: detección de ofuscador, devirtualización, etc.) no
estaba incluido.

Mientras tanto, `api/deobfuscate.py` usa una **transformación de
demostración** (decodifica escapes simples, reindenta por bloques) y lo
deja bien claro en la UI y en el log de cada corrida — **no es
deofuscación real**.

Para conectar el motor real:

1. Copia tu `deob.py` (y cualquier módulo que use) dentro de `deobf/`,
   reemplazando el `deobf/deob.py` placeholder.
2. Implementa la función `run(source, obfuscator, no_devirt, timeout)` con
   la firma y el `dict` de retorno que ya documenta ese archivo
   (`output`, `log`, `detected_obfuscator`).
3. Listo — `api/deobfuscate.py` lo detecta e importa automáticamente, sin
   tocar nada más. La UI deja de mostrar el aviso de "motor no conectado".

## Desarrollo local

```bash
npm install
npm run dev
```

La función Python (`api/deobfuscate.py`) solo corre bajo el runtime de
Vercel. Para probarla localmente con el mismo comportamiento de
producción usa la CLI de Vercel:

```bash
npm i -g vercel
vercel dev
```

## Variables de entorno

Copia `.env.example` a `.env.local` (local) o configúralas en **Vercel →
Project → Settings → Environment Variables** (producción):

| Variable              | Descripción                                                        |
| ---------------------- | ------------------------------------------------------------------- |
| `DISCORD_WEBHOOK_URL`  | Webhook de Discord. Se notifica inicio, fin (éxito) y fin (error). |

Si la dejas vacía, la app funciona igual pero sin notificar a Discord.

## Deploy en Vercel

```bash
vercel        # preview
vercel --prod # producción
```

O conecta el repo desde el dashboard de Vercel (detecta Next.js
automáticamente; la función Python en `api/` se despliega sola gracias a
`api/requirements.txt`). No olvides configurar `DISCORD_WEBHOOK_URL` en
las Environment Variables del proyecto antes del primer deploy en
producción.

**Nota sobre límites:** `vercel.json` pide 60s de `maxDuration` para
`api/deobfuscate.py`. En el plan Hobby de Vercel el máximo permitido puede
ser menor; si tus scripts tardan más, considera subir de plan o recortar
el timeout que expone la UI.

## Uso previsto

Herramienta educativa para el ámbito estudiantil: analizar y entender
técnicas de ofuscación en scripts Lua/Luau. No está pensada para eludir
protecciones en sistemas que no te pertenecen ni tienes autorización para
analizar.
