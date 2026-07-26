/**
 * NoSpeaky on-device engine
 * Whisper via Transformers.js (WebGPU → WASM). No server upload.
 */
import { pipeline, env } from "https://cdn.jsdelivr.net/npm/@huggingface/transformers@3.5.2";

env.allowLocalModels = false;
env.useBrowserCache = true;

const year = document.getElementById("y");
if (year) year.textContent = String(new Date().getFullYear());

const form = document.getElementById("job-form");
const fileInput = document.getElementById("file");
const drop = document.getElementById("drop");
const fileNameEl = document.getElementById("file-name");
const sourceLang = document.getElementById("source-lang");
const targetLang = document.getElementById("target-lang");
const modelSize = document.getElementById("model-size");
const startBtn = document.getElementById("start-btn");
const statusEl = document.getElementById("form-status");

const playerSection = document.getElementById("player-section");
const player = document.getElementById("player");
const cueOverlay = document.getElementById("cue-overlay");
const cueList = document.getElementById("cue-list");
const jobState = document.getElementById("job-state");
const jobDetail = document.getElementById("job-detail");
const progressBar = document.getElementById("progress-bar");
const btnSrt = document.getElementById("btn-srt");
const btnVtt = document.getElementById("btn-vtt");

/** @type {{start:number,end:number,text:string}[]} */
let cues = [];
let objectUrl = null;
let srtText = "";
let vttText = "";
let currentFile = null;
let asrPipeline = null;
let loadedModelId = null;
let busy = false;

function setStatus(msg, kind) {
  statusEl.textContent = msg;
  statusEl.style.color =
    kind === "ok" ? "var(--accent)" :
    kind === "err" ? "var(--danger)" :
    "var(--warn)";
}

function setProgress(pct, state, detail) {
  progressBar.style.width = `${Math.max(0, Math.min(100, pct))}%`;
  if (state) jobState.textContent = state;
  if (detail !== undefined) jobDetail.textContent = detail;
}

function clearObjectUrl() {
  if (objectUrl) {
    URL.revokeObjectURL(objectUrl);
    objectUrl = null;
  }
}

function pad2(n) {
  return String(n).padStart(2, "0");
}

function srtTs(sec) {
  sec = Math.max(0, sec || 0);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = Math.floor(sec % 60);
  const ms = Math.round((sec - Math.floor(sec)) * 1000);
  return `${pad2(h)}:${pad2(m)}:${pad2(s)},${String(ms).padStart(3, "0")}`;
}

function vttTs(sec) {
  return srtTs(sec).replace(",", ".");
}

function buildSrt(list) {
  return list
    .map((c, i) => `${i + 1}\n${srtTs(c.start)} --> ${srtTs(c.end)}\n${c.text}\n`)
    .join("\n");
}

function buildVtt(list) {
  return (
    "WEBVTT\n\n" +
    list.map((c) => `${vttTs(c.start)} --> ${vttTs(c.end)}\n${c.text}\n`).join("\n")
  );
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fmtTime(sec) {
  const s = Math.max(0, sec || 0);
  const m = Math.floor(s / 60);
  const r = (s % 60).toFixed(1).padStart(4, "0");
  return `${m}:${r}`;
}

function renderCueList() {
  cueList.innerHTML = "";
  cues.forEach((c, idx) => {
    const li = document.createElement("li");
    li.innerHTML = `<span class="t">${fmtTime(c.start)}</span><span>${escapeHtml(c.text)}</span>`;
    li.addEventListener("click", () => {
      player.currentTime = c.start;
      player.play().catch(() => {});
    });
    li.dataset.idx = String(idx);
    cueList.appendChild(li);
  });
}

function applyCues(list) {
  cues = list || [];
  srtText = buildSrt(cues);
  vttText = buildVtt(cues);
  renderCueList();
  btnSrt.disabled = !cues.length;
  btnVtt.disabled = !cues.length;
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
    if (t >= cues[i].start && t <= cues[i].end) {
      active = cues[i];
      activeIdx = i;
      break;
    }
  }
  cueOverlay.textContent = active ? active.text : "";
  [...cueList.children].forEach((li, i) => li.classList.toggle("active", i === activeIdx));
}

function downloadText(filename, text, mime) {
  const blob = new Blob([text], { type: mime || "text/plain;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2500);
}

function setFile(file) {
  currentFile = file || null;
  startBtn.disabled = !file || busy;
  if (!file) {
    fileNameEl.hidden = true;
    fileNameEl.textContent = "";
    return;
  }
  fileNameEl.hidden = false;
  const mb = (file.size / (1024 * 1024)).toFixed(1);
  fileNameEl.textContent = `${file.name} · ${mb} MB`;

  clearObjectUrl();
  objectUrl = URL.createObjectURL(file);
  player.src = objectUrl;
  playerSection.hidden = false;
  applyCues([]);
  cueOverlay.textContent = "";
  setProgress(0, "File ready", "Hit Start — runs on this device");
  setStatus("File loaded. Tap Start when ready.", "ok");
}

/** Decode browser-supported audio to 16 kHz mono Float32Array */
async function decodeToMono16k(arrayBuffer) {
  const OfflineCtx = window.OfflineAudioContext || window.webkitOfflineAudioContext;
  const probeCtx = new (window.AudioContext || window.webkitAudioContext)();
  let decoded;
  try {
    decoded = await probeCtx.decodeAudioData(arrayBuffer.slice(0));
  } finally {
    await probeCtx.close().catch(() => {});
  }

  const duration = decoded.duration;
  const targetRate = 16000;
  const frames = Math.max(1, Math.ceil(duration * targetRate));
  const offline = new OfflineCtx(1, frames, targetRate);
  const src = offline.createBufferSource();

  // mixdown if multi-channel
  let buffer = decoded;
  if (decoded.numberOfChannels > 1) {
    const mixed = offline.createBuffer(1, decoded.length, decoded.sampleRate);
    const out = mixed.getChannelData(0);
    for (let ch = 0; ch < decoded.numberOfChannels; ch++) {
      const data = decoded.getChannelData(ch);
      for (let i = 0; i < data.length; i++) out[i] += data[i] / decoded.numberOfChannels;
    }
    buffer = mixed;
  }

  src.buffer = buffer;
  src.connect(offline.destination);
  src.start(0);
  const rendered = await offline.startRendering();
  return rendered.getChannelData(0);
}

/**
 * Extract audio from video using ffmpeg.wasm (CDN).
 * Falls back with a clear error if it can't load on this device.
 */
async function extractAudioWithFfmpeg(file, onProgress) {
  onProgress?.(12, "Loading audio extractor…");
  const { FFmpeg } = await import("https://cdn.jsdelivr.net/npm/@ffmpeg/ffmpeg@0.12.15/+esm");
  const { fetchFile, toBlobURL } = await import("https://cdn.jsdelivr.net/npm/@ffmpeg/util@0.12.2/+esm");

  const ffmpeg = new FFmpeg();
  ffmpeg.on("progress", ({ progress }) => {
    const p = 12 + Math.round(Math.min(1, progress || 0) * 18);
    onProgress?.(p, "Extracting audio…");
  });

  const base = "https://cdn.jsdelivr.net/npm/@ffmpeg/core@0.12.10/dist/esm";
  await ffmpeg.load({
    coreURL: await toBlobURL(`${base}/ffmpeg-core.js`, "text/javascript"),
    wasmURL: await toBlobURL(`${base}/ffmpeg-core.wasm`, "application/wasm"),
  });

  const inputName = "input" + (file.name.match(/\.[a-z0-9]+$/i)?.[0] || ".mp4");
  await ffmpeg.writeFile(inputName, await fetchFile(file));
  // 16 kHz mono wav for Whisper
  await ffmpeg.exec([
    "-i", inputName,
    "-vn",
    "-ac", "1",
    "-ar", "16000",
    "-f", "wav",
    "out.wav",
  ]);
  const data = await ffmpeg.readFile("out.wav");
  const wavBuffer = data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength);
  return decodeToMono16k(wavBuffer);
}

async function fileToAudio(file, onProgress) {
  onProgress?.(8, "Reading file…");
  const type = (file.type || "").toLowerCase();
  const name = (file.name || "").toLowerCase();
  const isAudio =
    type.startsWith("audio/") ||
    /\.(mp3|wav|m4a|aac|ogg|flac|webm)$/i.test(name);

  if (isAudio) {
    onProgress?.(15, "Decoding audio…");
    const buf = await file.arrayBuffer();
    try {
      return await decodeToMono16k(buf);
    } catch (err) {
      // some "audio/mp4" m4a still need ffmpeg
      onProgress?.(15, "Retrying with extractor…");
      return extractAudioWithFfmpeg(file, onProgress);
    }
  }

  // video path
  try {
    return await extractAudioWithFfmpeg(file, onProgress);
  } catch (err) {
    throw new Error(
      "Could not pull audio from this video on your device. Try a short MP3/M4A, or another browser. " +
        (err?.message || err)
    );
  }
}

async function getAsr(modelId, onProgress) {
  if (asrPipeline && loadedModelId === modelId) return asrPipeline;

  onProgress?.(35, "Loading speech model (first time may take a bit)…");

  // Prefer WebGPU, fall back to WASM
  let device = "wasm";
  try {
    if (navigator.gpu) {
      const adapter = await navigator.gpu.requestAdapter();
      if (adapter) device = "webgpu";
    }
  } catch (_) {
    device = "wasm";
  }

  try {
    asrPipeline = await pipeline("automatic-speech-recognition", modelId, {
      device,
      dtype: device === "webgpu" ? "fp32" : "q8",
    });
  } catch (err) {
    if (device === "webgpu") {
      onProgress?.(38, "WebGPU failed — using WASM…");
      asrPipeline = await pipeline("automatic-speech-recognition", modelId, {
        device: "wasm",
        dtype: "q8",
      });
      device = "wasm";
    } else {
      throw err;
    }
  }

  loadedModelId = modelId;
  onProgress?.(48, `Model ready (${device})`);
  return asrPipeline;
}

function normalizeSegments(result) {
  // transformers may return { text, chunks: [{text, timestamp:[s,e]}] } or plain text
  const chunks = result?.chunks;
  if (Array.isArray(chunks) && chunks.length) {
    return chunks
      .map((c) => {
        const ts = c.timestamp || [0, 0];
        let start = Number(ts[0] ?? 0);
        let end = Number(ts[1] ?? start + 2);
        if (!Number.isFinite(start)) start = 0;
        if (!Number.isFinite(end) || end <= start) end = start + 1.5;
        const text = String(c.text || "").trim();
        return text ? { start, end, text } : null;
      })
      .filter(Boolean);
  }

  const text = String(result?.text || result || "").trim();
  if (!text) return [];
  return [{ start: 0, end: Math.max(2, player.duration || 4), text }];
}

/** Optional tiny translation pipelines for a few pairs (loaded on demand). */
async function maybeTranslateCues(list, target) {
  if (!list.length) return list;
  if (target === "en" || target === "same") return list;

  // Map to Helsinki-NLP opus-mt style via Xenova ports where available
  const pairModels = {
    zh: "Xenova/opus-mt-en-zh",
    ja: "Xenova/opus-mt-en-jap",
    ko: "Xenova/opus-mt-tc-big-en-ko",
    es: "Xenova/opus-mt-en-es",
    fr: "Xenova/opus-mt-en-fr",
    de: "Xenova/opus-mt-en-de",
  };
  const model = pairModels[target];
  if (!model) return list;

  setProgress(88, "Translating…", "Loading translation model");
  let translator;
  try {
    translator = await pipeline("translation", model, { device: "wasm" });
  } catch (err) {
    setStatus(`Subtitles ready in English-ish source text; translate model failed: ${err.message || err}`, "err");
    return list;
  }

  const out = [];
  for (let i = 0; i < list.length; i++) {
    const c = list[i];
    setProgress(88 + Math.round((i / list.length) * 10), "Translating…", `${i + 1}/${list.length}`);
    try {
      const res = await translator(c.text);
      const translated = Array.isArray(res) ? res[0]?.translation_text : res?.translation_text;
      out.push({ ...c, text: (translated || c.text).trim() });
    } catch (_) {
      out.push(c);
    }
  }
  return out;
}

function pickWhisperOptions() {
  const source = sourceLang.value;
  const target = targetLang.value;

  // Whisper built-in translate → English only
  let task = "transcribe";
  let language = source === "auto" ? null : source;

  if (target === "en") {
    // If user wants English subs, use whisper translate when source may not be English
    if (source === "auto" || source !== "english") {
      task = "translate";
    } else {
      task = "transcribe";
      language = "english";
    }
  } else if (target === "same") {
    task = "transcribe";
  } else {
    // Other languages: get English (or source) first then MT
    // Prefer English pivot via translate for non-english speech
    if (source === "english") {
      task = "transcribe";
      language = "english";
    } else {
      task = "translate"; // → English, then MT to target
    }
  }

  return { task, language, target };
}

async function runJob() {
  if (!currentFile || busy) return;
  busy = true;
  startBtn.disabled = true;
  playerSection.hidden = false;
  applyCues([]);

  const onProgress = (pct, detail) => setProgress(pct, "Working", detail);

  try {
    setStatus("Running on your device…", "ok");
    const audio = await fileToAudio(currentFile, onProgress);
    const modelId = modelSize.value;
    const asr = await getAsr(modelId, onProgress);
    const { task, language, target } = pickWhisperOptions();

    onProgress(55, task === "translate" ? "Listening → English…" : "Listening…");

    // Chunked for longer files
    const result = await asr(audio, {
      return_timestamps: true,
      chunk_length_s: 30,
      stride_length_s: 5,
      task,
      language: language || undefined,
    });

    onProgress(85, "Building subtitles…");
    let segs = normalizeSegments(result);

    if (target !== "en" && target !== "same") {
      // Ensure English pivot text then MT
      segs = await maybeTranslateCues(segs, target);
    }

    applyCues(segs);
    setProgress(100, "Ready", `${segs.length} lines · on-device`);
    setStatus(
      segs.length
        ? `Ready — ${segs.length} lines. Play or download .srt.`
        : "Finished, but no speech detected.",
      segs.length ? "ok" : "err"
    );
  } catch (err) {
    console.error(err);
    setProgress(100, "Failed", "");
    setStatus(`Failed: ${err?.message || err}`, "err");
  } finally {
    busy = false;
    startBtn.disabled = !currentFile;
  }
}

// UI wiring
fileInput.addEventListener("change", () => {
  const f = fileInput.files && fileInput.files[0];
  setFile(f || null);
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
  const f = e.dataTransfer?.files?.[0];
  if (!f) return;
  const dt = new DataTransfer();
  dt.items.add(f);
  fileInput.files = dt.files;
  setFile(f);
});

form.addEventListener("submit", (e) => {
  e.preventDefault();
  runJob();
});

player.addEventListener("timeupdate", syncOverlay);
player.addEventListener("seeked", syncOverlay);

btnSrt.addEventListener("click", () => {
  if (srtText) downloadText("nospeaky.srt", srtText, "application/x-subrip");
});
btnVtt.addEventListener("click", () => {
  if (vttText) downloadText("nospeaky.vtt", vttText, "text/vtt");
});

// Feature detect
(async () => {
  const hasGpu = Boolean(navigator.gpu);
  setStatus(
    hasGpu
      ? "Ready — on-device mode (WebGPU available). Pick a short clip."
      : "Ready — on-device mode (WASM). Pick a short clip; keep the tab open.",
    "ok"
  );
})();
