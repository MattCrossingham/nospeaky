(() => {
  const year = document.getElementById("y");
  if (year) year.textContent = String(new Date().getFullYear());

  const form = document.getElementById("caption-form");
  if (!form) return;

  const urlInput = document.getElementById("url");
  const fileInput = document.getElementById("file");
  const drop = document.getElementById("drop");
  const fileName = document.getElementById("file-name");
  const submitBtn = document.getElementById("submit-btn");
  const status = document.getElementById("form-status");

  // Flip true when api.nospeaky.ai (or same-origin /api) is live.
  const ENGINE_LIVE = false;
  const API_BASE = ""; // e.g. "https://api.nospeaky.ai"

  function hasInput() {
    const hasUrl = Boolean(urlInput.value.trim());
    const hasFile = Boolean(fileInput.files && fileInput.files[0]);
    return hasUrl || hasFile;
  }

  function refresh() {
    submitBtn.disabled = !ENGINE_LIVE || !hasInput();
    if (!ENGINE_LIVE) {
      status.textContent = "Engine not connected yet — page shell is live.";
    } else if (!hasInput()) {
      status.textContent = "Add a file or direct media URL.";
    } else {
      status.textContent = "Ready.";
    }
  }

  function setFile(file) {
    if (!file) {
      fileName.hidden = true;
      fileName.textContent = "";
      refresh();
      return;
    }
    // Show selection in UI; FileList is read-only so we keep input's files when from picker.
    fileName.hidden = false;
    fileName.textContent = `${file.name} · ${Math.max(1, Math.round(file.size / 1024))} KB`;
    if (urlInput.value) urlInput.value = "";
    refresh();
  }

  urlInput.addEventListener("input", () => {
    if (urlInput.value.trim() && fileInput.value) {
      fileInput.value = "";
      setFile(null);
    }
    refresh();
  });

  fileInput.addEventListener("change", () => {
    setFile(fileInput.files && fileInput.files[0]);
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
    // Assign via DataTransfer so the input carries the file for later FormData.
    const dt = new DataTransfer();
    dt.items.add(file);
    fileInput.files = dt.files;
    setFile(file);
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!ENGINE_LIVE) {
      status.textContent = "Not yet — API comes next. Site shell only.";
      return;
    }
    if (!hasInput()) return;

    submitBtn.disabled = true;
    status.textContent = "Uploading…";

    try {
      const body = new FormData();
      const file = fileInput.files && fileInput.files[0];
      if (file) body.append("file", file);
      const url = urlInput.value.trim();
      if (url) body.append("url", url);

      const res = await fetch(`${API_BASE}/v1/transcribe`, {
        method: "POST",
        body,
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      status.textContent = data.message || "Done — check downloads.";
      if (data.vtt_url) window.open(data.vtt_url, "_blank", "noopener");
    } catch (err) {
      status.textContent = `Failed: ${err.message || err}`;
    } finally {
      refresh();
    }
  });

  refresh();
})();
