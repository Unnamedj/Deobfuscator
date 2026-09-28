(() => {
  const sourceEl = document.getElementById("source");
  const filenameLabel = document.getElementById("filename-label");
  const fileInput = document.getElementById("file-input");
  const uploadBtn = document.getElementById("upload-btn");
  const obfuscatorEl = document.getElementById("obfuscator");
  const noDevirtEl = document.getElementById("no-devirt");
  const timeoutEl = document.getElementById("timeout");
  const statusPill = document.getElementById("status-pill");
  const runBtn = document.getElementById("run-btn");
  const detectBtn = document.getElementById("detect-btn");
  const downloadBtn = document.getElementById("download-btn");
  const copyBtn = document.getElementById("copy-btn");
  const outputPane = document.getElementById("output-pane");
  const logPane = document.getElementById("log-pane");
  const tabBtns = document.querySelectorAll(".tab-btn");
  const engineBanner = document.getElementById("engine-banner");

  let filename = "script.lua";
  let polling = null;
  let lastOutput = "";
  let running = false;

  fetch("/api/health")
    .then((r) => r.json())
    .then((d) => {
      if (!d.engineAvailable) engineBanner.classList.add("show");
    })
    .catch(() => engineBanner.classList.add("show"));

  uploadBtn.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    const f = fileInput.files[0];
    if (!f) return;
    filename = f.name;
    filenameLabel.textContent = `Script (${filename})`;
    f.text().then((text) => (sourceEl.value = text));
  });

  tabBtns.forEach((btn) => {
    btn.addEventListener("click", () => {
      tabBtns.forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      const tab = btn.dataset.tab;
      outputPane.style.display = tab === "output" ? "block" : "none";
      logPane.style.display = tab === "log" ? "block" : "none";
    });
  });

  function showTab(tab) {
    tabBtns.forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
    outputPane.style.display = tab === "output" ? "block" : "none";
    logPane.style.display = tab === "log" ? "block" : "none";
  }

  function setStatus(text, kind) {
    statusPill.textContent = text;
    statusPill.className = "status-pill" + (kind ? " " + kind : "");
  }

  function setRunning(isRunning) {
    running = isRunning;
    runBtn.disabled = isRunning;
    detectBtn.disabled = isRunning;
  }

  async function run(action) {
    if (running) return;
    const source = sourceEl.value;
    if (!source.trim()) {
      setStatus("Pega o sube un script primero.", "error");
      return;
    }

    setRunning(true);
    setStatus(action === "detect" ? "Detectando…" : "Encolando…", "running");
    outputPane.textContent = "";
    logPane.textContent = "";
    downloadBtn.disabled = true;
    copyBtn.disabled = true;
    lastOutput = "";

    let jobId;
    try {
      const res = await fetch("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action,
          source,
          filename,
          obfuscator: obfuscatorEl.value,
          noDevirt: noDevirtEl.checked,
          timeout: Number(timeoutEl.value) || 90,
        }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || "No se pudo crear el trabajo.");
      }
      const data = await res.json();
      jobId = data.id;
    } catch (err) {
      setStatus(err.message || "Error al conectar con el servidor.", "error");
      setRunning(false);
      return;
    }

    pollJob(jobId, action);
  }

  function pollJob(jobId, action) {
    if (polling) clearInterval(polling);
    polling = setInterval(async () => {
      let data;
      try {
        const res = await fetch(`/api/jobs/${jobId}`);
        data = await res.json();
      } catch {
        return; // transient network hiccup, keep polling
      }

      logPane.textContent = (data.log || []).join("\n");
      logPane.scrollTop = logPane.scrollHeight;

      if (data.status === "running" || data.status === "pending") {
        setStatus(
          action === "detect" ? "Detectando…" : "Deofuscando… (puede tardar varios minutos)",
          "running"
        );
        return;
      }

      clearInterval(polling);
      polling = null;
      setRunning(false);

      if (data.status === "done") {
        if (action === "detect") {
          setStatus(`Detección lista → ${data.detectedObfuscator || "?"}`, "done");
          showTab("log");
        } else {
          lastOutput = data.output || "";
          outputPane.textContent = lastOutput || "// (salida vacía)";
          downloadBtn.disabled = !lastOutput;
          copyBtn.disabled = !lastOutput;
          setStatus(`Listo (${data.elapsedMs} ms)`, "done");
          showTab("output");
        }
      } else {
        setStatus(data.error || "Falló. Revisa el registro.", "error");
        showTab("log");
      }
    }, 1500);
  }

  runBtn.addEventListener("click", () => run("deobfuscate"));
  detectBtn.addEventListener("click", () => run("detect"));

  downloadBtn.addEventListener("click", () => {
    if (!lastOutput) return;
    const ext = filename.includes(".") ? filename.split(".").pop() : "lua";
    const base = filename.replace(/\.[^.]+$/, "") || "output";
    const blob = new Blob([lastOutput], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${base}.deobf.${ext}`;
    a.click();
    URL.revokeObjectURL(url);
  });

  copyBtn.addEventListener("click", async () => {
    if (!lastOutput) return;
    try {
      await navigator.clipboard.writeText(lastOutput);
      const original = copyBtn.textContent;
      copyBtn.textContent = "¡Copiado!";
      setTimeout(() => (copyBtn.textContent = original), 1500);
    } catch {
      /* clipboard unavailable, ignore */
    }
  });
})();
