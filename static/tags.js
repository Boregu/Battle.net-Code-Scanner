const els = {
  tagModeGame: document.getElementById("tagModeGame"),
  tagModeCard: document.getElementById("tagModeCard"),
  tagModeChips: document.getElementById("tagModeChips"),
  tagModeSkipBtn: document.getElementById("tagModeSkipBtn"),
  tagModeSaveBtn: document.getElementById("tagModeSaveBtn"),
  tagReloadBtn: document.getElementById("tagReloadBtn"),
  tagQueueRemaining: document.getElementById("tagQueueRemaining"),
  tagSessionDone: document.getElementById("tagSessionDone"),
  tagCurrentInfo: document.getElementById("tagCurrentInfo"),
  tagError: document.getElementById("tagError"),
};

let tagModeQueue = [];
let tagModeIndex = 0;
let tagModeTotal = 0;
let tagModeOptions = {};
let tagModeSelected = new Set();
let tagModeSaving = false;
let tagSessionDone = 0;
let loadRequestId = 0;

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function imageUrl(item) {
  if (!item?.image_path && !item?.image_url) return null;
  if (item.image_path) return assetUrl(item.image_path.replace(/\\/g, "/"));
  return item.image_url;
}

function formatApiError(data, fallback) {
  if (typeof data?.error === "string") return data.error;
  if (typeof data?.detail === "string") return data.detail;
  return fallback || "Request failed";
}

async function api(path, options = {}) {
  const res = await fetch(apiUrl(path), {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (res.status === 405 || res.status === 404) {
      throw new Error("Tag API not found — restart run.bat to load the latest scanner server.");
    }
    throw new Error(formatApiError(data, res.statusText) || `Request failed (${res.status})`);
  }
  return data;
}

function setTagError(message) {
  if (!els.tagError) return;
  if (!message) {
    els.tagError.textContent = "";
    els.tagError.classList.add("hidden");
    return;
  }
  els.tagError.textContent = message;
  els.tagError.classList.remove("hidden");
}

function setTagLoading(loading) {
  if (els.tagModeSaveBtn) els.tagModeSaveBtn.disabled = loading || tagModeSaving;
  if (els.tagModeSkipBtn) els.tagModeSkipBtn.disabled = loading;
  if (els.tagReloadBtn) els.tagReloadBtn.disabled = loading;
  if (loading && els.tagCurrentInfo) els.tagCurrentInfo.textContent = "Loading…";
}

function updateTagStats() {
  const item = tagModeQueue[tagModeIndex];
  const remaining = Math.max(0, tagModeTotal - tagModeIndex);
  if (els.tagQueueRemaining) {
    els.tagQueueRemaining.textContent = Number.isFinite(tagModeTotal)
      ? remaining.toLocaleString()
      : "—";
  }
  if (els.tagSessionDone) {
    els.tagSessionDone.textContent = tagSessionDone.toLocaleString();
  }
  if (els.tagCurrentInfo) {
    if (!item) {
      els.tagCurrentInfo.textContent = tagModeTotal ? "Done" : "Empty";
      return;
    }
    els.tagCurrentInfo.textContent = `#${item.code.toLocaleString()} · ${item.game || "Unknown"}`;
  }
}

function tagModeGameFilter() {
  return els.tagModeGame?.value || "";
}

function tagModeOptionsForItem(item) {
  return tagModeOptions[item?.game || ""] || [];
}

function renderTagModeChips(item) {
  if (!els.tagModeChips) return;
  const options = tagModeOptionsForItem(item);
  if (!options.length) {
    els.tagModeChips.innerHTML = `<div class="empty-state compact">No tag options for this game.</div>`;
    return;
  }
  els.tagModeChips.innerHTML = options
    .map(
      (opt) =>
        `<button type="button" class="tag-mode-chip${tagModeSelected.has(opt.id) ? " is-selected" : ""}" data-tag="${escapeHtml(opt.id)}">${escapeHtml(opt.label)}</button>`,
    )
    .join("");
}

function renderTagModeCard() {
  if (!els.tagModeCard) return;
  const item = tagModeQueue[tagModeIndex];
  updateTagStats();

  if (!item) {
    els.tagModeCard.innerHTML = `<div class="empty-state">All caught up — no untagged products in this filter.</div>`;
    if (els.tagModeChips) els.tagModeChips.innerHTML = "";
    if (els.tagModeSaveBtn) els.tagModeSaveBtn.disabled = true;
    if (els.tagModeSkipBtn) els.tagModeSkipBtn.disabled = true;
    return;
  }

  if (els.tagModeSaveBtn) els.tagModeSaveBtn.disabled = tagModeSaving;
  if (els.tagModeSkipBtn) els.tagModeSkipBtn.disabled = false;

  tagModeSelected = new Set(item.catalog_tags || ["other"]);
  renderTagModeChips(item);

  const img = imageUrl(item);
  const suggested = (item.catalog_tag_labels || item.catalog_tags || ["other"]).join(", ");

  els.tagModeCard.innerHTML = `
    <div class="tag-mode-card-hero">
      ${
        img
          ? `<img src="${escapeHtml(img)}" alt="" loading="eager" decoding="async">`
          : `<span class="tag-mode-card-hero-empty">No image</span>`
      }
    </div>
    <div class="tag-mode-card-body">
      <div class="tag-mode-card-meta">
        <span>${escapeHtml(item.game || "Unknown")}</span>
        <span>#${item.code.toLocaleString()}</span>
      </div>
      <h2 class="tag-mode-card-title">${escapeHtml(item.name || "Unnamed product")}</h2>
      ${item.price ? `<p class="tag-mode-card-price">${escapeHtml(item.price)}</p>` : ""}
      ${item.includes ? `<p class="tag-mode-card-includes">${escapeHtml(item.includes)}</p>` : ""}
      <p class="tag-mode-card-suggested">Auto-detect: ${escapeHtml(suggested)}</p>
    </div>
  `;
}

async function loadTagModeQueue() {
  const requestId = ++loadRequestId;
  setTagError("");
  setTagLoading(true);
  if (els.tagModeCard) {
    els.tagModeCard.innerHTML = `<div class="empty-state">Loading queue…</div>`;
  }
  if (els.tagModeChips) els.tagModeChips.innerHTML = "";

  try {
    const game = tagModeGameFilter();
    const query = `/api/library/tag-queue?limit=80&offset=0${game ? `&game=${encodeURIComponent(game)}` : ""}`;
    const data = await api(query);
    if (requestId !== loadRequestId) return;

    tagModeQueue = data.items || [];
    tagModeTotal = typeof data.total === "number" ? data.total : tagModeQueue.length;
    tagModeIndex = 0;
    tagModeOptions = data.tag_options || {};
    renderTagModeCard();
  } catch (error) {
    if (requestId !== loadRequestId) return;
    setTagError(error.message);
    if (els.tagModeCard) {
      els.tagModeCard.innerHTML = `<div class="empty-state">Could not load tag queue.</div>`;
    }
    if (els.tagQueueRemaining) els.tagQueueRemaining.textContent = "—";
    if (els.tagCurrentInfo) els.tagCurrentInfo.textContent = "Error";
  } finally {
    if (requestId === loadRequestId) setTagLoading(false);
  }
}

async function saveTagModeAndNext() {
  if (tagModeSaving) return;
  const item = tagModeQueue[tagModeIndex];
  if (!item) return;
  if (!tagModeSelected.size) {
    setTagError("Pick at least one tag.");
    return;
  }

  tagModeSaving = true;
  setTagError("");
  if (els.tagModeSaveBtn) els.tagModeSaveBtn.disabled = true;
  try {
    const res = await api(`/api/library/${item.code}/catalog-tags`, {
      method: "PUT",
      body: JSON.stringify({ tags: [...tagModeSelected] }),
    });
    if (!res.ok) {
      setTagError(res.error || "Could not save tags.");
      return;
    }
    tagSessionDone += 1;
    tagModeTotal = Math.max(0, tagModeTotal - 1);
    tagModeIndex += 1;
    if (tagModeIndex >= tagModeQueue.length && tagModeTotal > 0) {
      await loadTagModeQueue();
    } else {
      renderTagModeCard();
    }
  } catch (error) {
    setTagError(error.message);
  } finally {
    tagModeSaving = false;
    if (els.tagModeSaveBtn) els.tagModeSaveBtn.disabled = !tagModeQueue[tagModeIndex];
  }
}

if (els.tagModeGame) {
  els.tagModeGame.addEventListener("change", () => void loadTagModeQueue());
}

if (els.tagReloadBtn) {
  els.tagReloadBtn.addEventListener("click", () => void loadTagModeQueue());
}

if (els.tagModeChips) {
  els.tagModeChips.addEventListener("click", (event) => {
    const btn = event.target.closest("[data-tag]");
    if (!btn) return;
    const tag = btn.dataset.tag;
    if (tagModeSelected.has(tag)) tagModeSelected.delete(tag);
    else tagModeSelected.add(tag);
    btn.classList.toggle("is-selected", tagModeSelected.has(tag));
    setTagError("");
  });
}

if (els.tagModeSaveBtn) {
  els.tagModeSaveBtn.addEventListener("click", () => void saveTagModeAndNext());
}

if (els.tagModeSkipBtn) {
  els.tagModeSkipBtn.addEventListener("click", () => {
    tagModeIndex += 1;
    renderTagModeCard();
  });
}

void loadTagModeQueue();
