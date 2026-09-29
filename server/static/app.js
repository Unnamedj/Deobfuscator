(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const el = {
    source: $("source"), fileMeta: $("file-meta"), fileInput: $("file-input"), srcStats: $("src-stats"),
    dropzone: $("dropzone"), samples: $("samples"), samplesWrap: $("samples-wrap"),
    timeout: $("timeout"), timeoutOut: $("timeout-out"), modeHint: $("mode-hint"),
    runBtn: $("run-btn"), detectBtn: $("detect-btn"), cancelBtn: $("cancel-btn"),
    progress: $("progress"), progressTitle: $("progress-title"), progressSub: $("progress-sub"),
    timer: $("timer"), stepper: $("stepper"), liveLine: $("live-line"),
    stats: $("stats"), copyBtn: $("copy-btn"), downloadBtn: $("download-btn"), wrapBtn: $("wrap-btn"),
    empty: $("empty-state"), code: $("code"), gutter: $("gutter"), codeBody: $("code-body"),
    outputView: $("output-view"), logView: $("log-view"), logBody: $("log-body"), logCount: $("log-count"),
    toasts: $("toasts"),
    pillEngine: $("pill-engine"), pillDiscord: $("pill-discord"), pillQueue: $("pill-queue"),
  };

  const PHASES = ["queued", "detect", "trace", "devirt", "finish"];
  const OBF_LABEL = { "auto-detect": "Auto", luraph_v15: "Luraph v15", ironbrew1: "IronBrew 1", generic: "Genérico" };
  const MODE_HINT = {
    full: "Devirtualiza el bytecode: recupera ramas y funciones completas. Puede tardar minutos.",
    fast: "Solo traza lo que el script hace al ejecutarse. Segundos, pero sin ramas no ejecutadas.",
  };
  const HIGHLIGHT_LIMIT = 400 * 1024;
  const JOB_KEY = "deobf.job";
  const PREFS_KEY = "deobf.prefs";

  let filename = "script.lua";
  let job = null;          // { id, action }
  let pollTimer = null;
  let tickTimer = null;
  let startedAt = 0;
  let output = "";
  let engineOk = false;

  const store = {
    get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
    del(k) { try { localStorage.removeItem(k); } catch { /* private mode */ } },
  };

  // ---------- helpers ----------
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const kb = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(2)} MB`);
  const clock = (ms) => { const s = Math.floor(ms / 1000); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
  const human = (ms) => (ms < 1000 ? `${ms} ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.floor(ms / 60000)} min ${Math.round((ms % 60000) / 1000)} s`);
  const radio = (name) => document.querySelector(`input[name="${name}"]:checked`).value;

  function toast(msg, kind = "") {
    const t = document.createElement("div");
    t.className = `toast ${kind}`;
    t.textContent = msg;
    el.toasts.appendChild(t);
    setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 220); }, 3200);
  }

  function setPill(pill, kind, text) {
    pill.className = `pill ${pill.classList.contains("hide-sm") ? "hide-sm " : ""}${kind}`;
    pill.querySelector("span").textContent = text;
  }

  // ---------- health / samples ----------
  async function refreshHealth() {
    try {
      const d = await (await fetch("/api/health")).json();
      engineOk = d.engineAvailable;
      setPill(el.pillEngine, d.engineAvailable ? "ok" : "err", d.engineAvailable ? "Motor en línea" : "Motor no disponible");
      setPill(el.pillDiscord, d.discord ? "ok" : "warn", d.discord ? "Discord conectado" : "Discord sin configurar");
      el.pillQueue.querySelector("span").textContent =
        d.running || d.queued ? `${d.running} en curso · ${d.queued} en cola` : "Sin cola";
    } catch {
      engineOk = false;
      setPill(el.pillEngine, "err", "Sin conexión");
    }
  }

  async function loadSamples() {
    try {
      const list = await (await fetch("/api/samples")).json();
      if (!list.length) return;
      for (const s of list) {
        const o = document.createElement("option");
        o.value = s.name;
        o.textContent = `${s.label} · ${s.name.replace(/^\d+_/, "").replace(/\.lua$/, "")}`;
        el.samples.appendChild(o);
      }
      el.samplesWrap.hidden = false;
    } catch { /* samples are optional */ }
  }

  async function loadSample(name) {
    try {
      const res = await fetch(`/api/samples/${encodeURIComponent(name)}`);
      if (!res.ok) throw new Error();
      setSource(await res.text(), name);
      toast("Ejemplo cargado", "ok");
    } catch {
      toast("No se pudo cargar el ejemplo", "err");
    }
  }

  // ---------- input ----------
  function setSource(text, name) {
    el.source.value = text;
    if (name) filename = name;
    updateSourceMeta();
  }

  function updateSourceMeta() {
    const text = el.source.value;
    const lines = text ? text.split("\n").length : 0;
    const size = new Blob([text]).size;
    el.srcStats.textContent = `${lines.toLocaleString("es")} líneas · ${kb(size)}`;
    el.fileMeta.textContent = text ? filename : "Pega el código o suelta un archivo";
  }

  function readFile(f) {
    if (!f) return;
    if (f.size > 2_000_000) return toast("El archivo supera los 2 MB", "err");
    f.text().then((t) => { setSource(t, f.name); toast(`${f.name} cargado`, "ok"); });
  }

  el.source.addEventListener("input", updateSourceMeta);
  el.source.addEventListener("keydown", (e) => {
    if (e.key === "Tab") {
      e.preventDefault();
      const { selectionStart: a, selectionEnd: b, value } = el.source;
      el.source.value = value.slice(0, a) + "\t" + value.slice(b);
      el.source.selectionStart = el.source.selectionEnd = a + 1;
    }
  });
  $("upload-btn").addEventListener("click", () => el.fileInput.click());
  el.fileInput.addEventListener("change", () => { readFile(el.fileInput.files[0]); el.fileInput.value = ""; });
  $("clear-btn").addEventListener("click", () => { filename = "script.lua"; setSource(""); el.source.focus(); });
  el.samples.addEventListener("change", () => { if (el.samples.value) loadSample(el.samples.value); el.samples.value = ""; });
  $("try-sample").addEventListener("click", (e) => {
    e.preventDefault();
    const first = el.samples.options[1];
    if (first) loadSample(first.value);
    else toast("No hay ejemplos en este servidor", "err");
  });

  let dragDepth = 0;
  el.dropzone.addEventListener("dragenter", (e) => { e.preventDefault(); dragDepth++; el.dropzone.classList.add("dragging"); });
  el.dropzone.addEventListener("dragover", (e) => e.preventDefault());
  el.dropzone.addEventListener("dragleave", () => { if (--dragDepth <= 0) { dragDepth = 0; el.dropzone.classList.remove("dragging"); } });
  el.dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    dragDepth = 0;
    el.dropzone.classList.remove("dragging");
    readFile(e.dataTransfer.files[0]);
  });

  // ---------- options ----------
  function savePrefs() {
    store.set(PREFS_KEY, { obf: radio("obf"), mode: radio("mode"), timeout: el.timeout.value });
  }
  function applyPrefs() {
    const p = store.get(PREFS_KEY);
    if (p) {
      const obf = document.querySelector(`input[name="obf"][value="${p.obf}"]`);
      const mode = document.querySelector(`input[name="mode"][value="${p.mode}"]`);
      if (obf) obf.checked = true;
      if (mode) mode.checked = true;
      if (p.timeout) el.timeout.value = p.timeout;
    }
    el.timeoutOut.textContent = `${el.timeout.value} s`;
    el.modeHint.textContent = MODE_HINT[radio("mode")];
  }
  document.querySelectorAll('input[name="obf"], input[name="mode"]').forEach((i) =>
    i.addEventListener("change", () => { el.modeHint.textContent = MODE_HINT[radio("mode")]; savePrefs(); })
  );
  el.timeout.addEventListener("input", () => { el.timeoutOut.textContent = `${el.timeout.value} s`; savePrefs(); });

  // ---------- tabs / viewer ----------
  function showTab(tab) {
    document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
    el.outputView.hidden = tab !== "output";
    el.logView.hidden = tab !== "log";
  }
  document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

  el.wrapBtn.addEventListener("click", () => {
    el.code.classList.toggle("wrap");
    el.wrapBtn.classList.toggle("active");
  });

  function renderOutput(text) {
    output = text || "";
    const has = Boolean(output);
    el.empty.hidden = has;
    el.code.hidden = !has;
    el.copyBtn.disabled = !has;
    el.downloadBtn.disabled = !has;
    if (!has) return;
    const n = output.split("\n").length;
    el.gutter.textContent = Array.from({ length: n }, (_, i) => i + 1).join("\n");
    el.codeBody.textContent = output;
    el.codeBody.removeAttribute("data-highlighted");
    if (window.hljs && output.length <= HIGHLIGHT_LIMIT) window.hljs.highlightElement(el.codeBody);
    el.outputView.scrollTop = 0;
  }

  function renderLog(lines) {
    el.logCount.textContent = lines.length ? String(lines.length) : "";
    el.logBody.innerHTML = lines.map((l) => {
      const cls = l.startsWith("[+]") ? "l-ok" : l.startsWith("[!]") ? "l-warn" : l.startsWith("$") ? "l-cmd" : "l-info";
      return `<span class="${cls}">${esc(l)}</span>`;
    }).join("\n");
  }

  function renderStats(d) {
    const chips = [];
    if (d.status === "done") chips.push(`<span class="stat ok">Completado</span>`);
    else if (d.status === "cancelled") chips.push(`<span class="stat">Cancelado</span>`);
    else chips.push(`<span class="stat err">Error</span>`);
    if (d.detectedObfuscator) {
      const conf = d.confidence != null ? ` · ${Math.round(d.confidence * 100)}%` : "";
      chips.push(`<span class="stat">Ofuscador <b>${esc(d.detectedObfuscator)}${conf}</b></span>`);
    }
    if (d.action === "deobfuscate") chips.push(`<span class="stat">Modo <b>${d.noDevirt ? "Rápido" : "Completo"}</b></span>`);
    chips.push(`<span class="stat">Tiempo <b>${human(d.elapsedMs)}</b></span>`);
    if (d.functions != null && d.output) chips.push(`<span class="stat">Funciones <b>${d.functions.toLocaleString("es")}</b></span>`);
    if (d.output) {
      chips.push(`<span class="stat">Líneas <b>${d.output.split("\n").length.toLocaleString("es")}</b></span>`);
      chips.push(`<span class="stat">Tamaño <b>${kb(new Blob([d.output]).size)}</b></span>`);
    }
    el.stats.innerHTML = chips.join("");
  }

  // ---------- progress ----------
  function renderProgress(d) {
    const skip = new Set();
    if (d.action === "detect") { skip.add("trace"); skip.add("devirt"); }
    else if (d.noDevirt) skip.add("devirt");
    const idx = PHASES.indexOf(d.phase);
    el.stepper.querySelectorAll("li").forEach((li, i) => {
      const p = li.dataset.phase;
      li.className = skip.has(p) ? "skipped" : i < idx ? "done" : i === idx ? "active" : "";
    });

    if (d.status === "queued") {
      el.progressTitle.textContent = d.queuePosition ? `En cola · posición ${d.queuePosition}` : "En cola…";
    } else {
      el.progressTitle.textContent = d.action === "detect" ? "Detectando ofuscador…" : {
        detect: "Detectando ofuscador…", trace: "Ejecutando en la VM de Luau…",
        devirt: "Devirtualizando bytecode…", finish: "Terminando…",
      }[d.phase] || "Procesando…";
    }
    el.progressSub.textContent = `${filename} · ${OBF_LABEL[d.obfuscator] || d.obfuscator}${d.detectedObfuscator ? ` → ${d.detectedObfuscator}` : ""}`;
    const last = [...d.log].reverse().find((l) => !l.startsWith("$"));
    el.liveLine.textContent = last || "Esperando al motor…";
    startedAt = Date.now() - d.elapsedMs;
  }

  function setBusy(busy) {
    el.runBtn.disabled = busy || !engineOk;
    el.detectBtn.disabled = busy || !engineOk;
    el.runBtn.classList.toggle("is-loading", busy);
    el.runBtn.querySelector(".run-label").textContent = busy ? "Procesando…" : "Deofuscar";
    el.progress.hidden = !busy;
    clearInterval(tickTimer);
    if (busy) tickTimer = setInterval(() => (el.timer.textContent = clock(Date.now() - startedAt)), 250);
  }

  // ---------- jobs ----------
  async function run(action) {
    if (job) return;
    if (!engineOk) return toast("El motor no está disponible", "err");
    const source = el.source.value;
    if (!source.trim()) { el.source.focus(); return toast("Pega o sube un script primero", "err"); }

    const body = {
      action, source, filename,
      obfuscator: radio("obf"), noDevirt: radio("mode") === "fast", timeout: Number(el.timeout.value),
    };
    startedAt = Date.now();
    el.timer.textContent = "0:00";
    renderProgress({ ...body, status: "queued", phase: "queued", log: [], elapsedMs: 0 });
    setBusy(true);
    el.progress.scrollIntoView({ behavior: "smooth", block: "nearest" });

    try {
      const res = await fetch("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "No se pudo crear el trabajo");
      job = { id: data.id, action };
      store.set(JOB_KEY, { ...job, filename });
      poll();
    } catch (err) {
      setBusy(false);
      toast(err.message || "Error de conexión", "err");
    }
  }

  async function poll() {
    clearTimeout(pollTimer);
    if (!job) return;
    let d;
    try {
      const res = await fetch(`/api/jobs/${job.id}`);
      if (res.status === 404) { finish(null); return toast("El trabajo expiró en el servidor", "err"); }
      d = await res.json();
    } catch {
      pollTimer = setTimeout(poll, 2500); // transient: server waking up / network blip
      return;
    }
    renderLog(d.log || []);
    if (d.status === "queued" || d.status === "running") {
      renderProgress(d);
      pollTimer = setTimeout(poll, 1200);
      return;
    }
    finish(d);
  }

  function finish(d) {
    job = null;
    store.del(JOB_KEY);
    setBusy(false);
    refreshHealth();
    if (!d) return;
    renderStats(d);
    if (d.status === "done") {
      if (d.action === "detect") {
        toast(`Detectado: ${d.detectedObfuscator || "desconocido"}`, "ok");
        showTab("log");
      } else {
        renderOutput(d.output);
        showTab("output");
        toast(`Listo en ${human(d.elapsedMs)}`, "ok");
      }
    } else if (d.status === "cancelled") {
      toast("Trabajo cancelado");
      showTab("log");
    } else {
      toast(d.error || "Falló — revisa el registro", "err");
      showTab("log");
    }
    $("result").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  el.runBtn.addEventListener("click", () => run("deobfuscate"));
  el.detectBtn.addEventListener("click", () => run("detect"));
  el.cancelBtn.addEventListener("click", async () => {
    if (!job) return;
    el.cancelBtn.disabled = true;
    try { await fetch(`/api/jobs/${job.id}/cancel`, { method: "POST" }); } catch { /* poll will tell */ }
    el.cancelBtn.disabled = false;
  });
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); run("deobfuscate"); }
  });

  // ---------- output actions ----------
  el.copyBtn.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(output); toast("Copiado al portapapeles", "ok"); }
    catch { toast("No se pudo copiar", "err"); }
  });
  el.downloadBtn.addEventListener("click", () => {
    const base = filename.replace(/\.[^.]+$/, "") || "output";
    const url = URL.createObjectURL(new Blob([output], { type: "text/plain;charset=utf-8" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: `${base}.deobf.luau` });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  // ---------- boot ----------
  applyPrefs();
  updateSourceMeta();
  loadSamples();
  refreshHealth().then(() => {
    const saved = store.get(JOB_KEY);
    if (saved && saved.id) {
      job = { id: saved.id, action: saved.action };
      if (saved.filename) filename = saved.filename;
      setBusy(true);
      toast("Retomando el trabajo en curso…");
      poll();
    } else {
      setBusy(false);
    }
  });
  setInterval(() => { if (!job) refreshHealth(); }, 20000);
})();
