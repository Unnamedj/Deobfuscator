"use client";

import { useEffect, useRef, useState } from "react";

const OBFUSCATORS = ["auto-detect", "luraph_v15", "ironbrew1", "generic"];

type RunResult = {
  ok: boolean;
  action: "deobfuscate" | "detect";
  output: string;
  log: string[];
  detectedObfuscator: string;
  engine: "real" | "stub";
  elapsedMs: number;
  error?: string;
};

type Status = "idle" | "running" | "done" | "error";

export default function Page() {
  const [source, setSource] = useState("");
  const [filename, setFilename] = useState("script.lua");
  const [obfuscator, setObfuscator] = useState(OBFUSCATORS[0]);
  const [noDevirt, setNoDevirt] = useState(false);
  const [timeout_, setTimeout_] = useState(90);
  const [status, setStatus] = useState<Status>("idle");
  const [statusText, setStatusText] = useState("Listo.");
  const [result, setResult] = useState<RunResult | null>(null);
  const [tab, setTab] = useState<"output" | "log">("output");
  const [engineAvailable, setEngineAvailable] = useState<boolean | null>(null);
  const [copyLabel, setCopyLabel] = useState("Copiar");
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    fetch("/api/deobfuscate")
      .then((r) => r.json())
      .then((d) => setEngineAvailable(Boolean(d.engineAvailable)))
      .catch(() => setEngineAvailable(false));
  }, []);

  function onFile(e: React.ChangeEvent<HTMLInputElement>) {
    const f = e.target.files?.[0];
    if (!f) return;
    setFilename(f.name);
    f.text().then(setSource);
  }

  async function run(action: "deobfuscate" | "detect") {
    if (status === "running") return;
    if (!source.trim()) {
      setStatusText("Pega o sube un script primero.");
      return;
    }
    setStatus("running");
    setStatusText(action === "detect" ? "Detectando…" : "Deofuscando…");
    setResult(null);

    try {
      const res = await fetch("/api/deobfuscate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action,
          source,
          filename,
          obfuscator,
          noDevirt,
          timeout: timeout_,
        }),
      });
      const data: RunResult = await res.json();
      setResult(data);
      if (data.ok) {
        setStatus("done");
        setStatusText(
          action === "detect"
            ? `Detección lista → ${data.detectedObfuscator}`
            : `Listo (${data.elapsedMs} ms)`
        );
        setTab(action === "detect" ? "log" : "output");
      } else {
        setStatus("error");
        setStatusText(data.error || "Falló. Revisa el registro.");
        setTab("log");
      }
    } catch (err) {
      setStatus("error");
      setStatusText("No se pudo contactar al servidor.");
    }
  }

  function download() {
    if (!result?.output) return;
    const ext = filename.includes(".") ? filename.split(".").pop() : "lua";
    const base = filename.replace(/\.[^.]+$/, "") || "output";
    const blob = new Blob([result.output], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${base}.deobf.${ext}`;
    a.click();
    URL.revokeObjectURL(url);
  }

  async function copyOutput() {
    if (!result?.output) return;
    try {
      await navigator.clipboard.writeText(result.output);
      setCopyLabel("¡Copiado!");
      setTimeout(() => setCopyLabel("Copiar"), 1500);
    } catch {
      setCopyLabel("No se pudo copiar");
      setTimeout(() => setCopyLabel("Copiar"), 1500);
    }
  }

  const statusColor =
    status === "running"
      ? "bg-warn/15 text-warn border-warn/30"
      : status === "done"
      ? "bg-ok/15 text-ok border-ok/30"
      : status === "error"
      ? "bg-err/15 text-err border-err/30"
      : "bg-muted/15 text-muted border-border";

  return (
    <main className="mx-auto flex min-h-screen max-w-6xl flex-col gap-6 px-4 py-8 sm:px-6">
      <header className="flex flex-col gap-2">
        <div className="flex items-center gap-3">
          <span className="text-2xl">🔓</span>
          <h1 className="text-xl font-semibold tracking-tight sm:text-2xl">
            Luau Deobfuscator
          </h1>
        </div>
        <p className="max-w-2xl text-sm text-muted">
          Herramienta educativa para analizar y limpiar scripts Lua/Luau
          ofuscados. Sube o pega el script, elige el ofuscador (o deja que se
          detecte solo) y obtén el código legible.
        </p>
        {engineAvailable === false && (
          <div className="rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-xs text-warn">
            ⚠️ Motor real no conectado todavía — esta demo usa una
            transformación de muestra. Conecta{" "}
            <code className="rounded bg-black/30 px-1 py-0.5">
              deobf/deob.py
            </code>{" "}
            para resultados reales.
          </div>
        )}
      </header>

      <section className="rounded-xl border border-border bg-panel p-4 sm:p-5">
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start">
          <div className="flex-1">
            <div className="mb-2 flex items-center justify-between">
              <label className="text-sm font-medium text-muted">
                Script ({filename})
              </label>
              <div className="flex gap-2">
                <button
                  onClick={() => fileInputRef.current?.click()}
                  className="rounded-lg border border-border bg-panel2 px-3 py-1.5 text-xs font-medium text-gray-100 hover:bg-border/60"
                >
                  Subir archivo…
                </button>
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".lua,.luau,.txt"
                  className="hidden"
                  onChange={onFile}
                />
              </div>
            </div>
            <textarea
              value={source}
              onChange={(e) => setSource(e.target.value)}
              placeholder="Pega aquí el script ofuscado…"
              spellCheck={false}
              className="h-56 w-full resize-y rounded-lg border border-border bg-bg p-3 font-mono text-xs leading-relaxed text-gray-100 outline-none focus:border-accent"
            />
          </div>
        </div>

        <div className="mt-4 flex flex-wrap items-end gap-4">
          <div className="flex flex-col gap-1">
            <label className="text-xs font-medium text-muted">
              Ofuscador
            </label>
            <select
              value={obfuscator}
              onChange={(e) => setObfuscator(e.target.value)}
              className="rounded-lg border border-border bg-panel2 px-3 py-2 text-sm outline-none focus:border-accent"
            >
              {OBFUSCATORS.map((o) => (
                <option key={o} value={o}>
                  {o}
                </option>
              ))}
            </select>
          </div>

          <label className="flex items-center gap-2 pb-2 text-sm">
            <input
              type="checkbox"
              checked={noDevirt}
              onChange={(e) => setNoDevirt(e.target.checked)}
              className="h-4 w-4 rounded border-border accent-accent"
            />
            Modo rápido (sin devirtualizar, solo trace)
          </label>

          <div className="flex flex-col gap-1">
            <label className="text-xs font-medium text-muted">
              Timeout (s)
            </label>
            <input
              type="number"
              min={10}
              max={600}
              value={timeout_}
              onChange={(e) => setTimeout_(Number(e.target.value))}
              className="w-24 rounded-lg border border-border bg-panel2 px-3 py-2 text-sm outline-none focus:border-accent"
            />
          </div>

          <div className="ml-auto flex items-center gap-3">
            <span
              className={`rounded-full border px-3 py-1 text-xs font-medium ${statusColor}`}
            >
              {statusText}
            </span>
          </div>
        </div>

        <div className="mt-4 flex flex-wrap gap-2">
          <button
            onClick={() => run("deobfuscate")}
            disabled={status === "running"}
            className="rounded-lg bg-accent2 px-4 py-2 text-sm font-semibold text-white transition hover:opacity-90 disabled:opacity-50"
          >
            {status === "running" ? "Procesando…" : "Deofuscar"}
          </button>
          <button
            onClick={() => run("detect")}
            disabled={status === "running"}
            className="rounded-lg border border-border bg-panel2 px-4 py-2 text-sm font-medium hover:bg-border/60 disabled:opacity-50"
          >
            Solo detectar
          </button>
          <button
            onClick={download}
            disabled={!result?.output}
            className="rounded-lg border border-border bg-panel2 px-4 py-2 text-sm font-medium hover:bg-border/60 disabled:opacity-50"
          >
            Descargar salida
          </button>
          <button
            onClick={copyOutput}
            disabled={!result?.output}
            className="rounded-lg border border-border bg-panel2 px-4 py-2 text-sm font-medium hover:bg-border/60 disabled:opacity-50"
          >
            {copyLabel}
          </button>
        </div>
      </section>

      <section className="flex-1 rounded-xl border border-border bg-panel p-4 sm:p-5">
        <div className="mb-3 flex gap-2 border-b border-border">
          {(["output", "log"] as const).map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`px-3 py-2 text-sm font-medium ${
                tab === t
                  ? "border-b-2 border-accent text-gray-100"
                  : "text-muted hover:text-gray-100"
              }`}
            >
              {t === "output" ? "Salida" : "Registro"}
            </button>
          ))}
        </div>

        {tab === "output" ? (
          <pre className="h-96 overflow-auto whitespace-pre rounded-lg bg-bg p-3 font-mono text-xs leading-relaxed text-gray-100">
            {result?.output || "// El código deofuscado aparecerá aquí."}
          </pre>
        ) : (
          <pre className="h-96 overflow-auto whitespace-pre-wrap rounded-lg bg-bg p-3 font-mono text-xs leading-relaxed text-muted">
            {result?.log?.join("\n") || "// El registro de la ejecución aparecerá aquí."}
          </pre>
        )}
      </section>

      <footer className="pb-4 text-center text-xs text-muted">
        Cada ejecución notifica inicio, fin y estado a un webhook de Discord
        (configurado por variable de entorno del servidor). Uso educativo /
        de investigación únicamente.
      </footer>
    </main>
  );
}
