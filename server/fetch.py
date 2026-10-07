"""
Download a script from a link, for jobs created with `url` instead of pasted text.

The server makes this request on behalf of whoever calls the API, so it is deliberately
narrow (SSRF): https only, host must be on an allowlist, redirects are followed by hand and
re-checked against the same rules, the body is capped and the wait is short.
Add hosts with FETCH_ALLOWED_HOSTS (comma separated).
"""

import os
import re
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_HOSTS = (
    "raw.githubusercontent.com",
    "gist.githubusercontent.com",
    "github.com",
    "gist.github.com",
    "gitlab.com",
    "pastebin.com",
    "cdn.discordapp.com",
    "media.discordapp.net",
)
MAX_REDIRECTS = 3
TIMEOUT_SECONDS = 20
USER_AGENT = "LuauDeobfuscator (web, 1.0)"


class FetchError(Exception):
    """A problem with the link itself; the message is meant for the user."""


def allowed_hosts():
    extra = os.environ.get("FETCH_ALLOWED_HOSTS", "")
    return set(DEFAULT_HOSTS) | {h.strip().lower() for h in extra.split(",") if h.strip()}


def normalize(url):
    """Validated https URL, with GitHub/Gist/Pastebin page links turned into their raw form."""
    url = (url or "").strip()
    if not url:
        raise FetchError("Pega un enlace.")
    if len(url) > 2000:
        raise FetchError("El enlace es demasiado largo.")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise FetchError("Solo se aceptan enlaces https://")
    host = (parts.hostname or "").lower()
    if parts.username or parts.password or parts.port not in (None, 443):
        raise FetchError("El enlace no puede llevar usuario, contraseña ni puerto.")
    if host not in allowed_hosts():
        raise FetchError("No se puede descargar de %s. Sitios permitidos: %s." %
                         (host or "?", ", ".join(sorted(allowed_hosts()))))

    path = parts.path
    m = re.fullmatch(r"/([^/]+)/([^/]+)/blob/(.+)", path) if host == "github.com" else None
    if m:  # github.com/<user>/<repo>/blob/<ref>/<file> -> raw.githubusercontent.com/<user>/<repo>/<ref>/<file>
        return "https://raw.githubusercontent.com/%s/%s/%s" % m.groups()
    m = re.fullmatch(r"/([^/]+)/([0-9a-f]+)/?", path) if host == "gist.github.com" else None
    if m:
        return "https://gist.githubusercontent.com/%s/%s/raw" % m.groups()
    m = re.fullmatch(r"/([A-Za-z0-9]{8})", path) if host == "pastebin.com" else None
    if m:
        return "https://pastebin.com/raw/%s" % m.group(1)
    return urllib.parse.urlunsplit(("https", host, path, parts.query, ""))


def display(url):
    """host/path without the query string: safe to log or show (signed links carry tokens there)."""
    p = urllib.parse.urlsplit(url)
    return (p.hostname or "") + p.path


def filename_for(url):
    name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not name or name == "raw":
        name = "script"
    return name if "." in name else name + ".lua"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def download(url, max_bytes):
    """(bytes, final_url): the response body exactly as served."""
    opener = urllib.request.build_opener(_NoRedirect)
    current = normalize(url)
    for _ in range(MAX_REDIRECTS + 1):
        req = urllib.request.Request(current, headers={"User-Agent": USER_AGENT, "Accept": "text/plain, */*"})
        try:
            with opener.open(req, timeout=TIMEOUT_SECONDS) as resp:
                body = resp.read(max_bytes + 1)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                current = normalize(urllib.parse.urljoin(current, exc.headers["Location"]))
                continue
            if exc.code == 404:
                raise FetchError("No se encontró el archivo (404). Revisa el enlace; si es de un repo privado no se puede leer.") from exc
            raise FetchError("El sitio respondió con error %d." % exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise FetchError("No se pudo descargar: %s" % getattr(exc, "reason", exc)) from exc
        if len(body) > max_bytes:
            raise FetchError("El archivo es demasiado grande (máx. %d KB)." % (max_bytes // 1000))
        if not body.strip():
            raise FetchError("El enlace devolvió un archivo vacío.")
        if re.match(rb"\s*<(!doctype html|html)", body[:200], re.I):
            raise FetchError("El enlace devuelve una página web, no el código. Usa el enlace del archivo en bruto (botón «Raw»).")
        return body, current
    raise FetchError("Demasiadas redirecciones.")
