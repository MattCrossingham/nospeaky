(() => {
  const year = document.getElementById("y");
  if (year) year.textContent = String(new Date().getFullYear());

  const form = document.getElementById("job-form");
  if (!form) return;

  // Local engine default. Override with ?api=https://host
  const params = new URLSearchParams(location.search);
  const API_BASE = (params.get("api") || "http://127.0.0.1:8788").replace(/\/$/, "");

  let engineLive = false;

  const urlInput = document.getElementById("url");
  const fileInput = document.getElementById("file");
  const drop = document.getElementById("drop");
  const fileName = document.getElementById("file-name");
  const sourceLang = document.getElementById("source-lang");
  const targetLang = document.getElementById("target-lang");
  const startBtn = document.getElementById("start-btn");
  const status = document.getElementById("form-status");

  const playerSection = document.getElementById("player-section");
  const player = document.getElementById("player");
  const cueOverlay = document.getElementById("cue-overlay");
  const cueList = document.getElementById("cue-list");
  const jobState = document.getElementById("job-state");
  const jobDetail = document.getElementById("job-detail");
  const progressBar = document.getElementById("progress-bar");
  const btnSrt = document.getElementById("btn-srt");
  const btnVtt = document.getElementById("btn-vtt");

  /** @type {{ start: number, end: number, text: string }[]} */
  let cues = [];
  let objectUrl = null;
  let srtText = "";
  let vttText = "";

  function hasInput() {
    return Boolean(urlInput.value.trim()) || Boolean(fileInput.files && fileInput.files[0]);
  }

  function setStatus(msg, kind) {
    status.textContent = msg;
    status.style.color =
      kind === "ok" ? "var(--accent)" :
      kind === "err" ? "var(--danger)" :
      "var(--warn)";
  }

  function setFileLabel(file) {
    if (!file) {
      fileName.hidden = true;
      fileName.textContent = "";
      return;
    }
    fileName.hidden = false;
    const kb = Math.max(1, Math.round(file.size / 1024));
    fileName.textContent = `${file.name} · ${kb} KB`;
  }

  function clearObjectUrl() {
    if (objectUrl) {
      URL.revokeObjectURL(objectUrl);
      objectUrl = null;
    }
  }

  function loadLocalFileIntoPlayer(file) {
    clearObjectUrl();
    objectUrl = URL.createObjectURL(file);
    player.src = objectUrl;
    playerSection.hidden = false;
    cues = [];
    srtText = "";
    vttText = "";
    cueList.innerHTML = "";
    cueOverlay.textContent = "";
    btnSrt.disabled = true;
    btnVtt.disabled = true;
    jobState.textContent = "File loaded";
    jobDetail.textContent = engineLive ? "Hit Start for subtitles" : "Player ready — start engine for subtitles";
    progressBar.style.width = "0%";
  }

  function fmtTime(sec) {
    const s = Math.max(0, sec || 0);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    const mm = String(m).padStart(2, "0");
    const ss = r.toFixed(3).padStart(6, "0");
    if (h > 0) return `${h}:${mm}:${ss}`;
    return `${m}:${ss.padStart(6, "0")}`;
  }

  function srtTimestamp(sec) {
    const s = Math.max(0, sec || 0);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const whole = Math.floor(s % 60);
    const ms = Math.round((s - Math.floor(s)) * 1000);
    return (
      String(h).padStart(2, "0") + ":" +
      String(m).padStart(2, "0") + ":" +
      String(whole).padStart(2, "0") + "," +
      String(ms).padStart(3, "0")
    );
  }

  function vttTimestamp(sec) {
    return srtTimestamp(sec).replace(",", ".");
  }

  function buildSrt(list) {
    return list.map((c, i) => (
      `${i + 1}\n${srtTimestamp(c.start)} --> ${srtTimestamp(c.end)}\n${c.text}\n`
    )).join("\n");
  }

  function buildVtt(list) {
    return "WEBVTT\n\n" + list.map((c) => (
      `${vttTimestamp(c.start)} --> ${vttTimestamp(c.end)}\n${c.text}\n`
    )).join("\n");
  }

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function renderCueList() {
    cueList.innerHTML = "";
    cues.forEach((c, idx) => {
      const li = document.createElement("li");
      li.dataset.idx = String(idx);
      li.innerHTML = `<span class="t">${fmtTime(c.start)}</span><span>${escapeHtml(c.text)}</span>`;
      li.addEventListener("click", () => {
        player.currentTime = c.start;
        player.play().catch(() => {});
      });
      cueList.appendChild(li);
    });
  }

  function applyCues(list) {
    cues = list || [];
    srtText = buildSrt(cues);
    vttText = buildVtt(cues);
    renderCueList();
    btnSrt.disabled = cues.length === 0 && !srtText;
    btnVtt.disabled = cues.length === 0 && !vttText;

    [...player.querySelectorAll("track")].forEach((t) => t.remove());
    if (cues.length) {
      const blob = new Blob([vttText], { type: "text/vtt" });
      const trackUrl = URL.createObjectURL(blob);
      const track = document.createElement("track");
      track.kind = "subtitles";
      track.label = targetLang.options[targetLang.selectedIndex].text;
      track.srclang = targetLang.value;
      track.src = trackUrl;
      track.default = true;
      player.appendChild(track);
      try {
        if (player.textTracks && player.textTracks[0]) {
          player.textTracks[0].mode = "hidden"; // we draw our own overlay
        }
      } catch (_) { /* ignore */ }
    }
  }

  function downloadText(filename, text, mime) {
    const blob = new Blob([text], { type: mime || "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }

  function syncOverlay() {
    if (!cues.length) {
      cueOverlay.textContent = "";
      return;
    }
    const t = player.currentTime || 0;
    let active = null;
    let activeIdx = -1;
    for (let i = 0; i < cues.length; i++) {
      const c = cues[i];
      if (t >= c.start && t <= c.end) {
        active = c;
        activeIdx = i;
        break;
      }
    }
    cueOverlay.textContent = active ? active.text : "";
    [...cueList.children].forEach((li, i) => {
      li.classList.toggle("active", i === activeIdx);
    });
  }

  async function probeEngine() {
    try {
      const res = await fetch(`${API_BASE}/health`, { method: "GET" });
      if (!res.ok) throw new Error("bad health");
      const data = await res.json();
      engineLive = Boolean(data && data.ok);
      if (engineLive) {
        setStatus(`Engine online (${API_BASE}). Paste a link or drop a file, then Start.`, "ok");
      }
    } catch (_) {
      engineLive = false;
      setStatus(
        `Engine offline. Start it on this Mac, then refresh. Looking for ${API_BASE}`,
        "err"
      );
    }
  }

  urlInput.addEventListener("input", () => {
    if (urlInput.value.trim() && fileInput.value) {
      fileInput.value = "";
      setFileLabel(null);
    }
  });

  fileInput.addEventListener("change", () => {
    const file = fileInput.files && fileInput.files[0];
    setFileLabel(file || null);
    if (file) {
      urlInput.value = "";
      loadLocalFileIntoPlayer(file);
      setStatus(
        engineLive
          ? "File loaded in player. Hit Start to make subtitles."
          : "File loaded. Start the engine, then hit Start.",
        engineLive ? "ok" : undefined
      );
    }
  });

  ["dragenter", "dragover"].forEach((evt) => {
    drop.addEventListener(evt, (e) => {
      e.preventDefault();
      drop.classList.add("dragover");
    });
  });
  ["dragleave", "drop"].forEach((evt) => {
    drop.addEventListener(evt, (e) => {
      e.preventDefault();
      drop.classList.remove("dragover");
    });
  });
  drop.addEventListener("drop", (e) => {
    const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (!file) return;
    const dt = new DataTransfer();
    dt.items.add(file);
    fileInput.files = dt.files;
    setFileLabel(file);
    urlInput.value = "";
    loadLocalFileIntoPlayer(file);
    setStatus(
      engineLive
        ? "File loaded in player. Hit Start to make subtitles."
        : "File loaded. Start the engine, then hit Start.",
      engineLive ? "ok" : undefined
    );
  });

  player.addEventListener("timeupdate", syncOverlay);
  player.addEventListener("seeked", syncOverlay);

  btnSrt.addEventListener("click", () => {
    if (!srtText) return;
    downloadText("nospeaky.srt", srtText, "application/x-subrip");
  });
  btnVtt.addEventListener("click", () => {
    if (!vttText) return;
    downloadText("nospeaky.vtt", vttText, "text/vtt");
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!hasInput()) {
      setStatus("Paste a link or drop a file first.", "err");
      return;
    }

    await probeEngine();
    if (!engineLive) {
      setStatus(`Engine not running at ${API_BASE}. Start it, then try again.`, "err");
      return;
    }

    const file = fileInput.files && fileInput.files[0];
    startBtn.disabled = true;
    playerSection.hidden = false;
    jobState.textContent = "Working";
    jobDetail.textContent = "Sending job…";
    progressBar.style.width = "5%";
    setStatus("Starting…", "ok");

    try {
      const body = new FormData();
      if (file) body.append("file", file);
      const url = urlInput.value.trim();
      if (url) body.append("url", url);
      body.append("source_lang", sourceLang.value);
      body.append("target_lang", targetLang.value);

      const res = await fetch(`${API_BASE}/v1/jobs`, { method: "POST", body });
      if (!res.ok) {
        let detail = `Server error ${res.status}`;
        try {
          const err = await res.json();
          detail = err.detail || err.error || detail;
        } catch (_) { /* ignore */ }
        throw new Error(detail);
      }
      const job = await res.json();
      await pollJob(job.id || job.job_id);
    } catch (err) {
      jobState.textContent = "Failed";
      jobDetail.textContent = err.message || String(err);
      setStatus(`Failed: ${err.message || err}`, "err");
    } finally {
      startBtn.disabled = false;
    }
  });

  async function pollJob(id) {
    for (;;) {
      const res = await fetch(`${API_BASE}/v1/jobs/${encodeURIComponent(id)}`);
      if (!res.ok) throw new Error(`Status check failed (${res.status})`);
      const job = await res.json();
      const progress = Number(job.progress || 0);
      progressBar.style.width = `${Math.min(100, Math.max(0, progress))}%`;
      jobState.textContent = job.status || "Working";
      jobDetail.textContent = job.message || "";

      if (job.media_url && !fileInput.files?.length) {
        // URL jobs: play media served by engine
        const mediaSrc = job.media_url.startsWith("http")
          ? job.media_url
          : `${API_BASE}${job.media_url}`;
        if (player.src !== mediaSrc) {
          clearObjectUrl();
          player.src = mediaSrc;
        }
      }

      if (Array.isArray(job.cues) && job.cues.length) {
        applyCues(job.cues);
      }
      if (job.srt) {
        srtText = job.srt;
        btnSrt.disabled = false;
      }
      if (job.vtt) {
        vttText = job.vtt;
        btnVtt.disabled = false;
      }

      const st = String(job.status || "").toLowerCase();
      if (st === "ready" || st === "done" || st === "completed") {
        if (Array.isArray(job.cues)) applyCues(job.cues);
        if (job.srt) srtText = job.srt;
        if (job.vtt) vttText = job.vtt;
        btnSrt.disabled = !srtText;
        btnVtt.disabled = !vttText;
        progressBar.style.width = "100%";
        jobState.textContent = "Ready";
        const n = (job.cues && job.cues.length) || 0;
        setStatus(
          n
            ? `Ready — ${n} lines. Play the video or download .srt.`
            : "Ready, but no speech was detected.",
          n ? "ok" : undefined
        );
        return;
      }
      if (st === "failed" || st === "error") {
        throw new Error(job.error || job.message || "Job failed");
      }
      await new Promise((r) => setTimeout(r, 1200));
    }
  }

  probeEngine();
})();
