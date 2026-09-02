(() => {
  const year = document.getElementById("y");
  if (year) year.textContent = String(new Date().getFullYear());

  const form = document.getElementById("job-form");
  if (!form) return;

  const params = new URLSearchParams(location.search);
  const cfg = window.NOSPEAKY_CONFIG || {};
  const API_BASE = (
    params.get("api") ||
    cfg.apiBase ||
    "https://api.nospeaky.ai"
  ).replace(/\/$/, "");
  const API_KEY = params.get("key") || cfg.apiKey || "";
  const PRO_TOKEN = (params.get("pro") || "").trim();

  let engineLive = false;
  let proReady = false;
  let mediaObjectJob = null;

  const urlInput = document.getElementById("url");
  const fileInput = document.getElementById("file");
  const drop = document.getElementById("drop");
  const fileName = document.getElementById("file-name");
  const sourceLang = document.getElementById("source-lang");
  const targetLang = document.getElementById("target-lang");
  (function fillLangs() {
    const langs = window.NOSPEAKY_LANGS || [];
    if (!langs.length) return;
    if (sourceLang) {
      sourceLang.innerHTML = "";
      const auto = document.createElement("option");
      auto.value = "auto";
      auto.textContent = "Auto-detect";
      auto.selected = true;
      sourceLang.appendChild(auto);
      langs.forEach((l) => {
        const o = document.createElement("option");
        o.value = l.code;
        o.textContent = l.name;
        sourceLang.appendChild(o);
      });
    }
    if (targetLang) {
      targetLang.innerHTML = "";
      const same = document.createElement("option");
      same.value = "same";
      same.textContent = "Same as spoken";
      targetLang.appendChild(same);
      langs.forEach((l) => {
        const o = document.createElement("option");
        o.value = l.code;
        o.textContent = l.name;
        if (l.code === "en") o.selected = true;
        targetLang.appendChild(o);
      });
    }
  })();
  const startBtn = document.getElementById("start-btn");
  const proBtn = document.getElementById("pro-btn");
  const tierInput = document.getElementById("tier");
  const status = document.getElementById("form-status");

  const playerSection = document.getElementById("player-section");
  const playerWrap = document.getElementById("player-wrap");
  const player = document.getElementById("player");
  const embed = document.getElementById("embed");
  const cueOverlay = document.getElementById("cue-overlay");
  const fsBtn = document.getElementById("fs-btn");
  const liveSub = document.getElementById("live-sub");
  const waitOverlay = document.getElementById("wait-overlay");
  const waitLabel = document.getElementById("wait-label");
  const waitTime = document.getElementById("wait-time");
  const waitHint = document.getElementById("wait-hint");
  const failOverlay = document.getElementById("fail-overlay");
  const failTitle = document.getElementById("fail-title");
  const failBody = document.getElementById("fail-body");
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
  let embedClock = 0;
  let embedTime = 0;
  let hasEmbedTime = false;
  let overlayRaf = 0;
  let waiting = false;
  let playingSubs = false;
  let etaUntil = 0;
  let waitTick = 0;
  let nativeTrack = false;
  let queueNote = "";

  const embedHost = document.getElementById("embed-host");
  let dmPlayer = null;
  let dmVideoId = "";
  let ytPlayer = null;
  let ytVideoId = "";
  let embedPing = 0;
  let embedPlaying = false;
  let lastTick = 0;

  function ytId(u) {
    const m = (u || "").match(/(?:youtube\.com\/watch\?v=|youtu\.be\/)([\w-]{6,})/i);
    return m ? m[1] : "";
  }

  function daiId(u) {
    const m = (u || "").match(/(?:dai\.ly\/|dailymotion\.com\/video\/)([A-Za-z0-9]+)/i);
    return m ? m[1] : "";
  }

  function embedFromLink(u) {
    if (!u) return "";
    const id = daiId(u);
    if (id) return "https://www.dailymotion.com/embed/video/" + id + "?autoplay=1&api=postMessage&origin=" + encodeURIComponent(location.origin);
    const y = u.match(/(?:youtube\.com\/watch\?v=|youtu\.be\/)([\w-]{6,})/i);
    if (y) return "https://www.youtube.com/embed/" + y[1] + "?autoplay=1&enablejsapi=1&fs=0";
    return "";
  }

  function cleanCue(text) {
    const words = String(text || "").trim().split(/\s+/).filter(Boolean);
    if (!words.length) return "";
    const out = [];
    let prev = "";
    let n = 0;
    words.forEach((w) => {
      const key = w.toLowerCase().replace(/[.,!?;:"']+/g, "");
      if (key === prev) {
        n += 1;
        if (n <= 2) out.push(w);
      } else {
        prev = key;
        n = 1;
        out.push(w);
      }
    });
    const uniq = new Set(out.map((w) => w.toLowerCase().replace(/[.,!?;:"']+/g, "")));
    if (uniq.size <= 1 && out.length >= 4) return "";
    if (out.length >= 12 && uniq.size <= 2) return "";
    return out.join(" ");
  }

  function setPlayhead(sec) {
    const t = Number(sec);
    if (!Number.isFinite(t) || t < 0) return;
    embedTime = t;
    hasEmbedTime = true;
    lastTick = performance.now();
  }

  function loadDmLib() {
    return new Promise((resolve, reject) => {
      if (window.dailymotion && dailymotion.createPlayer) {
        resolve(window.dailymotion);
        return;
      }
      const existing = document.querySelector("script[data-ns-dm]");
      if (existing) {
        existing.addEventListener("load", () => resolve(window.dailymotion));
        existing.addEventListener("error", reject);
        return;
      }
      const s = document.createElement("script");
      s.src = "https://geo.dailymotion.com/libs/player.js";
      s.async = true;
      s.dataset.nsDm = "1";
      s.onload = () => resolve(window.dailymotion);
      s.onerror = reject;
      document.head.appendChild(s);
    });
  }

  function bindDmTime(p) {
    const onTime = (state) => {
      if (state == null) return;
      const t =
        (typeof state === "number" && state) ||
        state.videoTime ||
        state.time ||
        state.currentTime ||
        (state.video && (state.video.time || state.video.currentTime));
      if (typeof t === "number") setPlayhead(t);
    };
    const ev = (window.dailymotion && dailymotion.events) || {};
    ["VIDEO_TIMECHANGE", "VIDEO_SEEKEND", "VIDEO_PLAYING", "VIDEO_PLAY", "VIDEO_PAUSE", "timechange", "timeupdate", "play", "pause"].forEach((name) => {
      try {
        const key = ev[name] || name;
        if (p.on) p.on(key, (state) => {
          const n = String(name).toLowerCase();
          if (n.includes("pause")) embedPlaying = false;
          else if (n.includes("play")) embedPlaying = true;
          onTime(state);
        });
      } catch (_) { /* ignore */ }
    });
    dmPlayer = p;
  }

  function pingEmbedTime() {
    if (ytPlayer && typeof ytPlayer.getCurrentTime === "function") {
      try {
        const t = ytPlayer.getCurrentTime();
        if (typeof t === "number") setPlayhead(t);
      } catch (_) { /* ignore */ }
      return;
    }
    if (dmPlayer) {
      try {
        if (typeof dmPlayer.getState === "function") {
          const st = dmPlayer.getState();
          if (st && typeof st.then === "function") {
            st.then((s) => {
              const t = s && (s.videoTime || s.time || s.currentTime);
              if (typeof t === "number") setPlayhead(t);
            }).catch(() => {});
          } else if (st) {
            const t = st.videoTime || st.time || st.currentTime;
            if (typeof t === "number") setPlayhead(t);
          }
        }
      } catch (_) { /* ignore */ }
      return;
    }
    if (!embed || embed.hidden || !embed.contentWindow) return;
    try {
      embed.contentWindow.postMessage(JSON.stringify({ command: "time" }), "*");
    } catch (_) { /* ignore */ }
  }

  function seekEmbed(sec) {
    const t = Number(sec);
    if (!Number.isFinite(t)) return;
    setPlayhead(t);
    if (ytPlayer && typeof ytPlayer.seekTo === "function") {
      try { ytPlayer.seekTo(t, true); } catch (_) { /* ignore */ }
      return;
    }
    if (dmPlayer && typeof dmPlayer.seek === "function") {
      try { dmPlayer.seek(t); } catch (_) { /* ignore */ }
      return;
    }
    if (!embed || embed.hidden || !embed.contentWindow) return;
    try {
      embed.contentWindow.postMessage(JSON.stringify({ command: "seek", parameters: [t] }), "*");
    } catch (_) { /* ignore */ }
  }

  function destroyYt() {
    if (ytPlayer && typeof ytPlayer.destroy === "function") {
      try { ytPlayer.destroy(); } catch (_) { /* ignore */ }
    }
    ytPlayer = null;
    ytVideoId = "";
  }

  function destroyDm() {
    destroyYt();
    if (dmPlayer && typeof dmPlayer.destroy === "function") {
      try { dmPlayer.destroy(); } catch (_) { /* ignore */ }
    }
    dmPlayer = null;
    dmVideoId = "";
    if (embedHost) {
      embedHost.innerHTML = "";
      embedHost.hidden = true;
    }
  }

  function showDai(id) {
    destroyDm();
    if (embed) {
      embed.hidden = true;
      embed.removeAttribute("src");
    }
    player.hidden = true;
    player.setAttribute("hidden", "");
    if (embedHost) {
      embedHost.hidden = false;
      embedHost.removeAttribute("hidden");
    }
    if (playerWrap) playerWrap.classList.add("has-embed");
    if (cueOverlay) cueOverlay.style.display = "";
    embedTime = 0;
    hasEmbedTime = true;
    embedPlaying = true;
    lastTick = performance.now();
    dmVideoId = id;
    loadDmLib().then((dm) => {
      if (!dm || !dm.createPlayer || dmVideoId !== id) return;
      return dm.createPlayer("embed-host", {
        video: id,
        params: { autoplay: true, mute: false, fullscreen: false }
      });
    }).then((p) => {
      if (!p || dmVideoId !== id) return;
      bindDmTime(p);
      try { if (p.play) p.play(); } catch (_) { /* ignore */ }
    }).catch(() => {
      if (embed) {
        embed.src = embedFromLink("https://dai.ly/" + id);
        embed.hidden = false;
        embed.removeAttribute("hidden");
      }
    });
    if (embedPing) clearInterval(embedPing);
    embedPing = setInterval(pingEmbedTime, 200);
  }

  function loadYtApi() {
    return new Promise((resolve, reject) => {
      if (window.YT && window.YT.Player) {
        resolve(window.YT);
        return;
      }
      const prev = window.onYouTubeIframeAPIReady;
      window.onYouTubeIframeAPIReady = function () {
        if (typeof prev === "function") prev();
        resolve(window.YT);
      };
      if (document.querySelector("script[data-ns-yt]")) return;
      const s = document.createElement("script");
      s.src = "https://www.youtube.com/iframe_api";
      s.async = true;
      s.dataset.nsYt = "1";
      s.onerror = reject;
      document.head.appendChild(s);
    });
  }

  function showYouTube(id) {
    destroyDm();
    if (embed) {
      embed.hidden = true;
      embed.removeAttribute("src");
    }
    player.hidden = true;
    player.setAttribute("hidden", "");
    if (embedHost) {
      embedHost.hidden = false;
      embedHost.removeAttribute("hidden");
      embedHost.innerHTML = "";
    }
    if (playerWrap) playerWrap.classList.add("has-embed");
    if (cueOverlay) cueOverlay.style.display = "";
    embedTime = 0;
    hasEmbedTime = true;
    embedPlaying = true;
    lastTick = performance.now();
    ytVideoId = id;
    loadYtApi().then((YT) => {
      if (!YT || !YT.Player || ytVideoId !== id) return;
      ytPlayer = new YT.Player("embed-host", {
        videoId: id,
        width: "100%",
        height: "100%",
        playerVars: { autoplay: 1, playsinline: 1, rel: 0, fs: 0, origin: location.origin },
        events: {
          onReady: (e) => {
            embedPlaying = true;
            setPlayhead(0);
            try { e.target.playVideo(); } catch (_) { /* ignore */ }
          },
          onStateChange: (e) => {
            const st = window.YT && window.YT.PlayerState;
            if (!st) return;
            if (e.data === st.PLAYING) embedPlaying = true;
            if (e.data === st.PAUSED || e.data === st.ENDED) embedPlaying = false;
            try { setPlayhead(e.target.getCurrentTime()); } catch (_) { /* ignore */ }
          }
        }
      });
    }).catch(() => {
      if (embed) {
        embed.src = "https://www.youtube.com/embed/" + id + "?autoplay=1&enablejsapi=1&fs=0&origin=" + encodeURIComponent(location.origin);
        embed.hidden = false;
        embed.removeAttribute("hidden");
      }
    });
    if (embedPing) clearInterval(embedPing);
    embedPing = setInterval(pingEmbedTime, 200);
  }

  function showEmbed(eu) {
    const yid = ytId(urlInput && urlInput.value) || ytId(eu);
    if (yid) {
      showYouTube(yid);
      return;
    }
    const id = daiId(urlInput && urlInput.value) || daiId(eu);
    if (id) {
      showDai(id);
      return;
    }
    if (!embed || !eu) return;
    destroyDm();
    if (embed.src !== eu) {
      embed.src = eu;
      embedClock = 0;
      embedTime = 0;
      hasEmbedTime = false;
      embed.onload = () => {
        setTimeout(() => {
          try {
            embed.contentWindow.postMessage(
              '{"command":"subscribe","parameters":["timeupdate","playing","progress","seeked","seeking"]}',
              "*"
            );
          } catch (_) { /* ignore */ }
          pingEmbedTime();
        }, 400);
      };
    }
    embed.hidden = false;
    embed.removeAttribute("hidden");
    player.hidden = true;
    player.setAttribute("hidden", "");
    if (playerWrap) playerWrap.classList.add("has-embed");
    if (cueOverlay) cueOverlay.style.display = "";
    if (embedPing) clearInterval(embedPing);
    embedPing = setInterval(pingEmbedTime, 200);
  }

  function fmtLeft(sec) {
    const n = Math.max(0, Math.ceil(sec || 0));
    const m = Math.floor(n / 60);
    const s = n % 60;
    return m + ":" + String(s).padStart(2, "0");
  }

  function paintWait() {
    if (!waitOverlay || waitOverlay.hidden) return;
    if (!etaUntil) {
      if (waitTime) waitTime.textContent = "…";
      if (waitHint) waitHint.textContent = "Working out how long this will take.";
      return;
    }
    const left = (etaUntil - Date.now()) / 1000;
    if (waitTime) waitTime.textContent = fmtLeft(left);
    if (waitHint) {
      if (queueNote) waitHint.textContent = queueNote;
      else if (left > 0) waitHint.textContent = "Subtitles start when this hits zero.";
      else waitHint.textContent = "Almost there…";
    }
  }

  function friendlyError(raw) {
    const s = String(raw || "");
    const low = s.toLowerCase();
    if (!s.trim() || low.includes("paste a link")) {
      return { title: "Nothing to translate", body: "Paste a public video link first." };
    }
    if (low.includes("engine not") || low.includes("warming") || low.includes("failed to fetch") || low.includes("networkerror") || low.includes("load failed")) {
      return { title: "Translator is down", body: "Wait a minute and try again." };
    }
    if (low.includes("youtube") || low.includes("sign in") || low.includes("bot") || low.includes("confirm you’re not") || low.includes("confirm you're not")) {
      return { title: "YouTube blocked this clip", body: "Try another public YouTube link, Dailymotion, or a direct .mp4." };
    }
    if (low.includes("geo") || low.includes("region") || low.includes("not available") || low.includes("unavailable")) {
      return { title: "This video is locked", body: "Region-locked or private clips won’t work. Need a public link." };
    }
    if (low.includes("unsupported") || low.includes("unable to extract") || low.includes("no video") || low.includes("yt-dlp") || low.includes("impersonate")) {
      return { title: "We can’t open that link", body: "Use Dailymotion (dai.ly) or a direct .mp4 / .m4a." };
    }
    if (low.includes("already streaming") || low.includes("wait a minute")) {
      return { title: "Someone else is using the live slot", body: "This box runs one stream. Wait until that clip finishes, then Translate again." };
    }
    if (low.includes("too many") || (low.includes("limit") && low.includes("hour"))) {
      return { title: "Slow down", body: "Too many translates from here. Try again in a bit." };
    }
    if (low.includes("too long") || low.includes("duration")) {
      return { title: "Clip is too long", body: "Free is for short clips. Try a shorter video." };
    }
    if (low.includes("no speech") || low.includes("no cue")) {
      return { title: "No speech found", body: "We opened it, but didn’t hear words." };
    }
    return { title: "Didn’t work", body: s };
  }

  function hideFail() {
    if (failOverlay) failOverlay.hidden = true;
  }

  function showFail(raw) {
    const f = friendlyError(raw);
    hideWait();
    playerSection.hidden = false;
    if (failOverlay) failOverlay.hidden = false;
    if (failTitle) failTitle.textContent = f.title;
    if (failBody) failBody.textContent = f.body;
    jobState.textContent = "Failed";
    jobDetail.textContent = f.title;
    setStatus(f.title + " — " + f.body, "err");
  }

  function showWait(etaSec, label) {
    waiting = true;
    playingSubs = false;
    hideFail();
    playerSection.hidden = false;
    if (waitOverlay) waitOverlay.hidden = false;
    if (waitLabel) waitLabel.textContent = label || "Translating";
    if (typeof etaSec === "number" && Number.isFinite(etaSec) && etaSec >= 0) {
      etaUntil = Date.now() + etaSec * 1000;
    }
    if (cueOverlay) cueOverlay.textContent = "";
    paintWait();
    if (!waitTick) waitTick = setInterval(paintWait, 250);
  }

  function hideWait() {
    waiting = false;
    if (waitOverlay) waitOverlay.hidden = true;
    if (waitTick) {
      clearInterval(waitTick);
      waitTick = 0;
    }
  }

  function apiHeaders(extra) {
    const h = Object.assign({}, extra || {});
    if (API_KEY) h["X-NoSpeaky-Key"] = API_KEY;
    return h;
  }

  async function apiFetch(path, options) {
    const opts = options || {};
    opts.headers = apiHeaders(opts.headers || {});
    return fetch(`${API_BASE}${path}`, opts);
  }

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
    mediaObjectJob = null;
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

  async function loadRemoteMedia(jobId) {
    if (mediaObjectJob === jobId && player.src) return;
    const res = await apiFetch(`/v1/jobs/${encodeURIComponent(jobId)}/media`);
    if (!res.ok) throw new Error(`Media fetch failed (${res.status})`);
    const blob = await res.blob();
    clearObjectUrl();
    objectUrl = URL.createObjectURL(blob);
    mediaObjectJob = jobId;
    player.src = objectUrl;
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
    if (!cueList) return;
    cues.forEach((c, idx) => {
      const li = document.createElement("li");
      li.dataset.idx = String(idx);
      li.innerHTML = `<span class="t">${fmtTime(c.start)}</span><span>${escapeHtml(c.text)}</span>`;
      li.addEventListener("click", () => {
        seekEmbed(c.start);
        player.currentTime = c.start;
        player.play().catch(() => {});
        syncOverlay();
      });
      cueList.appendChild(li);
    });
  }

  function applyCues(list, native) {
    nativeTrack = Boolean(native);
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
      track.srclang = targetLang.value === "same" ? "und" : targetLang.value;
      track.src = trackUrl;
      track.default = true;
      player.appendChild(track);
      try {
        if (player.textTracks && player.textTracks[0]) {
          player.textTracks[0].mode = "hidden";
        }
      } catch (_) { /* ignore */ }
    }
  }

  function showHtml5() {
    if (embedPing) {
      clearInterval(embedPing);
      embedPing = 0;
    }
    if (typeof destroyDm === "function") destroyDm();
    if (embed) {
      embed.hidden = true;
      embed.setAttribute("hidden", "");
      embed.removeAttribute("src");
    }
    player.hidden = false;
    player.removeAttribute("hidden");
    if (playerWrap) playerWrap.classList.remove("has-embed");
    if (cueOverlay) cueOverlay.style.display = "";
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
    if (!playingSubs || !cues.length) {
      if (cueOverlay) cueOverlay.textContent = "";
      if (liveSub) liveSub.textContent = "";
      return;
    }
    const usingEmbed = Boolean(ytPlayer) || Boolean(dmPlayer) || (embedHost && !embedHost.hidden) || (embed && !embed.hidden);
    let t = player.currentTime || 0;
    if (usingEmbed) {
      t = hasEmbedTime ? embedTime : -1;
    }
    let active = null;
    let activeIdx = -1;
    if (t >= 0) {
      for (let i = cues.length - 1; i >= 0; i--) {
        const c = cues[i];
        if (t >= c.start && t <= (c.end + 0.4)) {
          active = c;
          activeIdx = i;
          break;
        }
      }
    }
    cueOverlay.textContent = active ? cleanCue(active.text) : "";
    if (liveSub) liveSub.textContent = active ? active.text : "";
    if (cueList) {
      [...cueList.children].forEach((li, i) => {
        li.classList.toggle("active", i === activeIdx);
      });
    }
  }

  window.addEventListener("message", (ev) => {
    let d = ev.data;
    if (typeof d === "string") {
      try { d = JSON.parse(d); } catch { return; }
    }
    if (!d || typeof d !== "object") return;
    let t = null;
    if (typeof d.time === "number") t = d.time;
    else if (typeof d.currentTime === "number") t = d.currentTime;
    else if (d.info && typeof d.info.currentTime === "number") t = d.info.currentTime;
    else if (Array.isArray(d.parameters) && typeof d.parameters[0] === "number") t = d.parameters[0];
    if (typeof t === "number" && Number.isFinite(t) && t >= 0) {
      setPlayhead(t);
      embedPlaying = true;
    }
  });

  function tickOverlay(now) {
    now = typeof now === "number" ? now : performance.now();
    if (embedPlaying && hasEmbedTime) {
      const dt = (now - lastTick) / 1000;
      if (dt > 0 && dt < 0.5) embedTime += dt;
    }
    lastTick = now;
    syncOverlay();
    overlayRaf = requestAnimationFrame(tickOverlay);
  }
  tickOverlay();

  async function probeEngine() {
    try {
      const res = await fetch(`${API_BASE}/health`, { method: "GET" });
      if (!res.ok) throw new Error("bad health");
      const data = await res.json();
      engineLive = Boolean(data && data.ok);
      proReady = Boolean(data && data.pro_ready);
      const canPro = engineLive && proReady && Boolean(PRO_TOKEN);
      if (proBtn) {
        proBtn.hidden = !PRO_TOKEN;
        proBtn.disabled = !canPro;
      }
      if (engineLive) {
        setStatus(
          canPro
            ? "Paste a link, pick a language, then Translate or Pro."
            : "Paste a link, pick a language, then Translate.",
          "ok"
        );
      }
    } catch (_) {
      engineLive = false;
      proReady = false;
      if (proBtn) {
        proBtn.hidden = !PRO_TOKEN;
        proBtn.disabled = true;
      }
      setStatus(
        "Translator is warming up. Try again in a minute.",
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
          : "File loaded. Engine must be online to make subtitles.",
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
        : "File loaded. Engine must be online to make subtitles.",
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


  function wsUrl() {
    return API_BASE.replace(/^http/i, "ws").replace(/\/$/, "") + "/v1/stream";
  }

  function runStream(url, tier) {
    return new Promise((resolve, reject) => {
      let sock;
      try { sock = new WebSocket(wsUrl()); }
      catch (e) { reject(e); return; }
      let got = false;
      const timer = setTimeout(() => {
        if (!got) {
          try { sock.close(); } catch (_) {}
          reject(new Error("Live captions timed out"));
        }
      }, 45000);
      sock.onopen = () => {
        sock.send(JSON.stringify({
          url: url,
          source_lang: sourceLang.value,
          target_lang: targetLang.value,
          tier: tier,
          key: API_KEY,
          pro_token: PRO_TOKEN
        }));
      };
      sock.onerror = () => reject(new Error("Live captions failed"));
      sock.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (_) { return; }
        if (msg.type === "error") {
          try { sock.close(); } catch (_) {}
          reject(new Error(msg.error || "stream"));
          return;
        }
        if (msg.type === "status") {
          jobDetail.textContent = msg.message || "Streaming";
          setStatus(msg.message || "Live captions…", "ok");
        }
        if (msg.type === "cue" && msg.text) {
          got = true;
          const txt = cleanCue(msg.text);
          if (!txt) return;
          cues.push({ start: Number(msg.start) || 0, end: Number(msg.end) || 0, text: txt });
          applyCues(cues, false);
          if (cues.length === 1) {
            hideWait();
            playingSubs = true;
            const eu = embedFromLink(url);
            if (eu) showEmbed(eu);
            jobState.textContent = "Playing";
            setStatus("Captions while it plays.", "ok");
          }
          progressBar.style.width = Math.min(90, 10 + cues.length * 4) + "%";
        }
        if (msg.type === "done") {
          clearTimeout(timer);
          if (Array.isArray(msg.cues) && msg.cues.length) applyCues(msg.cues, false);
          progressBar.style.width = "100%";
          try { sock.close(); } catch (_) {}
          resolve();
        }
      };
      sock.onclose = () => {
        clearTimeout(timer);
        if (got) resolve();
        else reject(new Error("Stream closed"));
      };
    });
  }

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!hasInput()) {
      showFail("Paste a link first.");
      return;
    }

    await probeEngine();
    if (!engineLive) {
      showFail("Engine not online. Can’t start a job yet.");
      return;
    }
    const tier = (tierInput && tierInput.value) || "free";
    if (tier === "pro" && (!proReady || !PRO_TOKEN)) {
      setStatus("Pro is locked.", "err");
      if (tierInput) tierInput.value = "free";
      return;
    }

    const file = fileInput.files && fileInput.files[0];
    startBtn.disabled = true;
    if (proBtn) proBtn.disabled = true;
    playerSection.hidden = false;
    showWait(null, tier === "pro" ? "Pro translating" : "Translating");
    jobState.textContent = "Working";
    jobDetail.textContent = "Starting live captions…";
    progressBar.style.width = "5%";
    setStatus(tier === "pro" ? "Pro translating…" : "Translating…", "ok");

    try {
      const url = urlInput.value.trim();
      if (url && !file) {
        try {
          await runStream(url, tier);
          return;
        } catch (streamErr) {
          const sm = String((streamErr && streamErr.message) || streamErr);
          if (/already streaming/i.test(sm) || /wait a minute/i.test(sm)) {
            showFail("This box is already streaming another clip. Wait a minute.");
            return;
          }
          jobDetail.textContent = "Live path missed. Whole-file job…";
        }
      }
      const body = new FormData();
      if (file) body.append("file", file);
      if (url) body.append("url", url);
      body.append("source_lang", sourceLang.value);
      body.append("target_lang", targetLang.value);
      body.append("tier", tier);
      if (tier === "pro" && PRO_TOKEN) body.append("pro_token", PRO_TOKEN);

      const res = await apiFetch("/v1/jobs", { method: "POST", body });
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
      showFail(err.message || String(err));
    } finally {
      startBtn.disabled = false;
      if (proBtn) {
        proBtn.hidden = !PRO_TOKEN;
        proBtn.disabled = !(engineLive && proReady && PRO_TOKEN);
      }
      if (tierInput) tierInput.value = "free";
    }
  });

  if (proBtn) {
    proBtn.addEventListener("click", () => {
      if (tierInput) tierInput.value = "pro";
      form.requestSubmit();
    });
  }

  async function startPlayback(job, id) {
    hideWait();
    playingSubs = true;
    const hasFile = Boolean(fileInput.files && fileInput.files[0]);
    if (job.media_url || hasFile) {
      try {
        if (job.media_url && !hasFile) await loadRemoteMedia(id);
        nativeTrack = false;
        showHtml5();
        applyCues(job.cues || cues, false);
        player.play().catch(() => {});
        return;
      } catch (_) {
        nativeTrack = false;
      }
    }
    applyCues(job.cues || cues, false);
    if (cueOverlay) cueOverlay.style.display = "";
    const link = urlInput.value.trim();
    const eu = job.embed_url || embedFromLink(link);
    if (eu) showEmbed(eu);
    else player.play().catch(() => {});
  }

  async function pollJob(id) {
    for (;;) {
      const res = await apiFetch(`/v1/jobs/${encodeURIComponent(id)}`);
      if (!res.ok) throw new Error(`Status check failed (${res.status})`);
      const job = await res.json();
      const progress = Number(job.progress || 0);
      progressBar.style.width = `${Math.min(100, Math.max(0, progress))}%`;
      jobState.textContent = job.status || "Working";
      jobDetail.textContent = job.message || "";

      if (waiting) {
        const qp = Number(job.queue_pos || 0);
        const ql = Number(job.queue_len || 0);
        if (qp > 1) {
          queueNote = "Number " + qp + " in the queue" + (ql ? " of " + ql : "") + ".";
          if (waitLabel) waitLabel.textContent = "In the queue";
        } else {
          queueNote = "";
          if (waitLabel) waitLabel.textContent = "Translating";
        }
        if (typeof job.eta_sec === "number" && Number.isFinite(job.eta_sec)) {
          etaUntil = Date.now() + Math.max(0, job.eta_sec) * 1000;
        } else if (typeof job.duration === "number" && job.duration > 0) {
          const left = job.duration * Math.max(0, 1 - progress / 100);
          etaUntil = Date.now() + left * 1000;
        }
        paintWait();
      }

      if (Array.isArray(job.cues) && job.cues.length) {
        cues = job.cues;
        srtText = job.srt || buildSrt(cues);
        vttText = job.vtt || buildVtt(cues);
        if (!waiting) applyCues(job.cues);
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
        await startPlayback(job, id);
        jobState.textContent = "Playing";
        const n = (job.cues && job.cues.length) || 0;
        if (!n) {
          showFail("no speech");
          return;
        }
        setStatus(`Playing with ${n} lines on the video.`, "ok");
        return;
      }
      if (st === "failed" || st === "error") {
        throw new Error(job.error || job.message || "Job failed");
      }
      await new Promise((r) => setTimeout(r, 400));
    }
  }

  function fsEl() {
    return document.fullscreenElement || document.webkitFullscreenElement || null;
  }

  function playerIsFs() {
    return Boolean(playerWrap && (playerWrap.classList.contains("is-fs") || fsEl() === playerWrap));
  }

  function syncFsUi() {
    const on = playerIsFs();
    if (document.body) document.body.classList.toggle("player-fs", on);
    if (!fsBtn) return;
    fsBtn.setAttribute("aria-pressed", on ? "true" : "false");
    fsBtn.setAttribute("aria-label", on ? "Exit full screen" : "Full screen");
  }

  function exitPlayerFs() {
    const el = fsEl();
    if (el) {
      const exit = document.exitFullscreen || document.webkitExitFullscreen;
      if (exit) {
        try { exit.call(document); } catch (_) { /* ignore */ }
      }
    }
    if (playerWrap) playerWrap.classList.remove("is-fs");
    syncFsUi();
  }

  function enterPlayerFs() {
    if (!playerWrap) return;
    playerWrap.classList.add("is-fs");
    syncFsUi();
  }

  function togglePlayerFs() {
    if (playerIsFs()) exitPlayerFs();
    else enterPlayerFs();
  }

  if (fsBtn && playerWrap) {
    fsBtn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      togglePlayerFs();
    });
    document.addEventListener("fullscreenchange", () => {
      if (fsEl() !== playerWrap) playerWrap.classList.remove("is-fs");
      syncFsUi();
    });
    document.addEventListener("webkitfullscreenchange", () => {
      if (fsEl() !== playerWrap) playerWrap.classList.remove("is-fs");
      syncFsUi();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && playerIsFs()) exitPlayerFs();
    });
  }

  probeEngine();
})();
