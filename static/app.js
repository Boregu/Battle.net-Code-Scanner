const els = {
  statusDot: document.getElementById("statusDot"),
  statusText: document.getElementById("statusText"),
  region: document.getElementById("region"),
  delayMs: document.getElementById("delayMs"),
  concurrency: document.getElementById("concurrency"),
  startCode: document.getElementById("startCode"),
  endCode: document.getElementById("endCode"),
  currentCode: document.getElementById("currentCode"),
  scanOneCode: document.getElementById("scanOneCode"),
  headless: document.getElementById("headless"),
  skipScanned: document.getElementById("skipScanned"),
  skipNoProduct: document.getElementById("skipNoProduct"),
  saveSettingsBtn: document.getElementById("saveSettingsBtn"),
  openLoginBtn: document.getElementById("openLoginBtn"),
  saveLoginBtn: document.getElementById("saveLoginBtn"),
  nextBtn: document.getElementById("nextBtn"),
  scanOneBtn: document.getElementById("scanOneBtn"),
  autoStartBtn: document.getElementById("autoStartBtn"),
  enrichIncompleteBtn: document.getElementById("enrichIncompleteBtn"),
  revalidateBtn: document.getElementById("revalidateBtn"),
  autoPauseBtn: document.getElementById("autoPauseBtn"),
  autoResumeBtn: document.getElementById("autoResumeBtn"),
  autoStopBtn: document.getElementById("autoStopBtn"),
  currentResult: document.getElementById("currentResult"),
  sessionElapsed: document.getElementById("sessionElapsed"),
  sessionAvgScan: document.getElementById("sessionAvgScan"),
  sessionRate: document.getElementById("sessionRate"),
  sessionEta: document.getElementById("sessionEta"),
  sessionProgress: document.getElementById("sessionProgress"),
  currentScanInfo: document.getElementById("currentScanInfo"),
  recentStrip: document.getElementById("recentStrip"),
  searchInput: document.getElementById("searchInput"),
  sortBy: document.getElementById("sortBy"),
  validOnly: document.getElementById("validOnly"),
  exportBtn: document.getElementById("exportBtn"),
  libraryScroll: document.getElementById("libraryScroll"),
  libraryVirtual: document.getElementById("libraryVirtual"),
  libraryGrid: document.getElementById("libraryGrid"),
  libraryStats: document.getElementById("libraryStats"),
};

let ws;
let busy = false;
let scannerStatus = "idle";
let sessionStartedAt = null;
let activeScans = new Map();
let sessionCodesDone = 0;
let sessionCodesTotal = 0;
let etaSeconds = null;
let avgSecondsPerScan = null;
let scansPerSecond = null;
let libraryTotal = 0;
let libraryCache = new Map();
let libraryQueryKey = "";
let libraryLoading = false;
let libraryRefreshTimer = null;
let libraryScrollRaf = null;
let searchDebounceTimer = null;
let lastLibraryRenderKey = "";
let pendingRecentRefresh = false;
let pendingCurrentResult = null;

const CHUNK_SIZE = 120;

function setTextIfChanged(el, text) {
  if (el && el.textContent !== text) el.textContent = text;
}

function setClassIfChanged(el, className) {
  if (el && el.className !== className) el.className = className;
}

function isUserSelecting() {
  const sel = window.getSelection();
  return Boolean(sel && !sel.isCollapsed && sel.toString().trim());
}

function selectionInside(container) {
  if (!container) return false;
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed) return false;
  const node = sel.anchorNode;
  return Boolean(node && container.contains(node));
}

function flushPendingUpdates() {
  if (isUserSelecting()) return;
  if (pendingCurrentResult) {
    renderCurrentResult(pendingCurrentResult);
    pendingCurrentResult = null;
  }
  if (pendingRecentRefresh) {
    pendingRecentRefresh = false;
    refreshRecent();
  }
}

document.addEventListener("mouseup", () => {
  setTimeout(() => {
    flushPendingUpdates();
    if (!isUserSelecting()) scheduleLibraryRender();
  }, 0);
});

function countMissingInRange(startIdx, endIdx) {
  let missing = 0;
  for (let i = startIdx; i < endIdx; i += 1) {
    if (!libraryCache.has(i)) missing += 1;
  }
  return missing;
}

function visibleRangeSignature(startIdx, endIdx) {
  const parts = [];
  for (let i = startIdx; i < endIdx; i += 1) {
    const item = getLibraryItem(i);
    parts.push(item ? `${item.code}:${item.checked_at || ""}` : `loading:${i}`);
  }
  return `${libraryQueryKey}|${startIdx}|${endIdx}|${VIRTUAL.cols}|${parts.join(",")}`;
}

function libraryQueryParams() {
  return {
    validOnly: els.validOnly.checked,
    search: els.searchInput.value.trim(),
    sort: els.sortBy.value,
  };
}

function libraryParamsKey(params) {
  return `${params.validOnly}|${params.search}|${params.sort}`;
}

function libraryApiQuery(params, extra = {}) {
  const q = new URLSearchParams({
    valid_only: String(params.validOnly),
    search: params.search,
    sort: params.sort,
    ...extra,
  });
  return q.toString();
}

async function ensureLibraryChunk(startIdx, count) {
  const params = libraryQueryParams();
  const key = libraryParamsKey(params);
  if (key !== libraryQueryKey) return;

  const endIdx = Math.min(libraryTotal, startIdx + count);
  const missing = [];
  for (let i = startIdx; i < endIdx; i += 1) {
    if (!libraryCache.has(i)) missing.push(i);
  }
  if (!missing.length) return;

  const chunkStart = missing[0];
  let chunkEnd = chunkStart + 1;
  while (chunkEnd < endIdx && missing.includes(chunkEnd)) {
    chunkEnd += 1;
  }
  const chunkCount = Math.min(CHUNK_SIZE, chunkEnd - chunkStart);

  const items = await api(`/api/library?${libraryApiQuery(params, {
    limit: String(chunkCount),
    offset: String(chunkStart),
  })}`);
  items.forEach((item, index) => {
    libraryCache.set(chunkStart + index, item);
  });
}

async function loadLibraryWindow(startIdx, endIdx) {
  if (!libraryTotal) return;
  const params = libraryQueryParams();
  libraryQueryKey = libraryParamsKey(params);
  const paddedStart = Math.max(0, startIdx - CHUNK_SIZE);
  const paddedEnd = Math.min(libraryTotal, endIdx + CHUNK_SIZE);
  for (let at = paddedStart; at < paddedEnd; at += CHUNK_SIZE) {
    await ensureLibraryChunk(at, CHUNK_SIZE);
  }
}

function getLibraryItem(index) {
  return libraryCache.get(index) || null;
}

function cardSkeletonHtml() {
  return `
    <article class="card invalid">
      <div class="card-thumb-placeholder"></div>
      <div class="card-body">
        <div class="code">Loading…</div>
      </div>
    </article>
  `;
}

const VIRTUAL = {
  cardHeight: 320,
  gap: 14,
  cols: 1,
  rowHeight: 334,
  bufferRows: 2,
};

function parseTime(iso) {
  if (!iso) return null;
  const value = new Date(iso).getTime();
  return Number.isFinite(value) ? value : null;
}

function formatDuration(ms) {
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const totalSec = Math.floor(ms / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (h > 0) return `${h}h ${m}m ${s}s`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function formatAvgScan(seconds) {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  if (seconds < 10) return `${seconds.toFixed(1)}s`;
  return `${Math.round(seconds)}s`;
}

function applyActivity(data) {
  if (data.session_started_at !== undefined) {
    sessionStartedAt = data.session_started_at;
  }
  if (data.active_scans) {
    activeScans = new Map(
      Object.entries(data.active_scans).map(([code, started]) => [Number(code), started]),
    );
  }
  if (data.session_codes_done !== undefined) {
    sessionCodesDone = data.session_codes_done;
  }
  if (data.session_codes_total !== undefined) {
    sessionCodesTotal = data.session_codes_total;
  }
  if (data.eta_seconds !== undefined) {
    etaSeconds = data.eta_seconds;
  }
  if (data.avg_seconds_per_scan !== undefined) {
    avgSecondsPerScan = data.avg_seconds_per_scan;
  }
  if (data.scans_per_second !== undefined) {
    scansPerSecond = data.scans_per_second;
  }
  updateSessionDisplay();
}

function updateSessionDisplay() {
  const now = Date.now();
  const sessionStart = parseTime(sessionStartedAt);
  setTextIfChanged(els.sessionElapsed, sessionStart ? formatDuration(now - sessionStart) : "—");

  if (sessionCodesTotal > 0) {
    setTextIfChanged(els.sessionProgress, `${sessionCodesDone} / ${sessionCodesTotal}`);
  } else if (sessionCodesDone > 0) {
    setTextIfChanged(els.sessionProgress, `${sessionCodesDone} scanned`);
  } else {
    setTextIfChanged(els.sessionProgress, "—");
  }

  if (avgSecondsPerScan != null && avgSecondsPerScan > 0) {
    setTextIfChanged(els.sessionAvgScan, formatAvgScan(avgSecondsPerScan));
  } else {
    setTextIfChanged(els.sessionAvgScan, "—");
  }

  if (scansPerSecond != null && scansPerSecond > 0 && scannerStatus === "running") {
    setTextIfChanged(els.sessionRate, `${scansPerSecond}/s`);
  } else {
    setTextIfChanged(els.sessionRate, "—");
  }

  if (etaSeconds != null && etaSeconds > 0 && scannerStatus === "running") {
    setTextIfChanged(els.sessionEta, formatDuration(etaSeconds * 1000));
  } else {
    setTextIfChanged(els.sessionEta, "—");
  }

  if (activeScans.size === 0) {
    setClassIfChanged(els.currentScanInfo, "header-stat-value idle");
    if (scannerStatus === "running") {
      setTextIfChanged(els.currentScanInfo, "Waiting…");
    } else if (scannerStatus === "paused") {
      setTextIfChanged(els.currentScanInfo, "Paused");
    } else {
      setTextIfChanged(els.currentScanInfo, "Idle");
    }
    return;
  }

  const entries = [...activeScans.entries()]
    .map(([code, started]) => ({
      code,
      elapsed: now - parseTime(started),
    }))
    .sort((a, b) => b.elapsed - a.elapsed);

  const longest = entries[0];
  let scanText;
  if (entries.length === 1) {
    scanText = `#${longest.code} · ${formatDuration(longest.elapsed)}`;
  } else {
    const preview = entries
      .slice(0, 2)
      .map((entry) => `#${entry.code} (${formatDuration(entry.elapsed)})`)
      .join(", ");
    const suffix = entries.length > 2 ? ` +${entries.length - 2}` : "";
    scanText = `${entries.length} active: ${preview}${suffix}`;
  }
  setTextIfChanged(els.currentScanInfo, scanText);
  setClassIfChanged(
    els.currentScanInfo,
    longest.elapsed >= 45000 ? "header-stat-value stuck" : "header-stat-value active",
  );
}

setInterval(updateSessionDisplay, 1000);

function imageUrl(item) {
  if (!item?.image_path && !item?.image_url) return null;
  if (item.image_path) return assetUrl(item.image_path.replace(/\\/g, "/"));
  return item.image_url;
}

function formatApiError(data, statusText) {
  const detail = data?.detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (typeof item === "string") return item;
        const field = Array.isArray(item.loc) ? item.loc.filter((part) => part !== "body").join(".") : "";
        const prefix = field ? `${field}: ` : "";
        return `${prefix}${item.msg || JSON.stringify(item)}`;
      })
      .join("\n");
  }
  if (typeof detail === "string") return detail;
  return statusText || "Request failed";
}

async function api(path, options = {}) {
  const res = await fetch(apiUrl(path), {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json();
  if (!res.ok) {
    throw new Error(formatApiError(data, res.statusText) || `Request failed (${res.status})`);
  }
  return data;
}

function setBusy(state) {
  busy = state;
  els.nextBtn.disabled = state;
  els.scanOneBtn.disabled = state;
  els.autoStartBtn.disabled = state;
  els.enrichIncompleteBtn.disabled = state;
  els.revalidateBtn.disabled = state;
}

function updateStatus(status, loggedIn) {
  scannerStatus = status;
  els.statusDot.className = "status-dot";
  if (status === "running") {
    els.statusDot.classList.add("running");
    els.statusText.textContent = "Auto scanning";
    els.autoPauseBtn.disabled = false;
    els.autoResumeBtn.disabled = true;
    els.autoStopBtn.disabled = false;
  } else if (status === "paused") {
    els.statusDot.classList.add("paused");
    els.statusText.textContent = "Paused";
    els.autoPauseBtn.disabled = true;
    els.autoResumeBtn.disabled = false;
    els.autoStopBtn.disabled = false;
  } else if (status === "needs_login") {
    els.statusDot.classList.add("needs_login");
    els.statusText.textContent = loggedIn ? "Session may have expired" : "Login required";
    resetAutoButtons();
  } else {
    els.statusText.textContent = loggedIn ? "Ready" : "Not logged in";
    resetAutoButtons();
  }
  updateSessionDisplay();
}

function resetAutoButtons() {
  els.autoPauseBtn.disabled = true;
  els.autoResumeBtn.disabled = true;
  els.autoStopBtn.disabled = true;
}

function applySettings(settings) {
  els.region.value = settings.region || "us";
  els.delayMs.value = settings.delay_ms ?? 2000;
  els.concurrency.value = settings.concurrency ?? 2;
  els.startCode.value = settings.start_code ?? 64300;
  els.endCode.value = settings.end_code ?? 64400;
  els.currentCode.value = settings.current_code ?? settings.start_code ?? 64300;
  els.headless.checked = settings.headless !== false;
  els.skipScanned.checked = settings.skip_scanned !== false;
  els.skipNoProduct.checked = settings.skip_no_product === true;
}

let sessionRecent = [];

function scanOutcome(item) {
  if (item?.valid) return "got_it";
  if (item?.status === "empty") return "no_product";
  const msg = (item?.message || "").toLowerCase();
  if (msg.includes("nothing here") || item?.message === "Product not found") return "no_product";
  return "failed";
}

function outcomeLabel(outcome) {
  return (
    {
      got_it: "Got it",
      no_product: "No product",
      failed: "Scan failed",
    }[outcome] || "Scan failed"
  );
}

function statusLabel(status) {
  if (status === "empty") return "No product";
  if (status === "valid" || status === "partial") return "Got it";
  if (["rate_limited", "error", "timeout", "server_error", "needs_login"].includes(status)) return "Scan failed";
  return "Scan failed";
}

function resultReason(item) {
  const outcome = scanOutcome(item);
  if (outcome === "got_it") return item.message || null;
  if (outcome === "no_product") {
    return null;
  }
  if (item.message) {
    if (/APIRequestContext|Connection closed|Max redirect|ERR_TOO_MANY_REDIRECTS|Scan error: Page\.goto/i.test(item.message)) {
      return "Could not verify — use fewer parallel scans, then Recheck Invalid";
    }
    if (item.message.startsWith("Network error:")) {
      return item.message.replace(/^Network error:\s*/, "Temporary error: ");
    }
    if (item.message.startsWith("Scan error:")) {
      return item.message.replace(/^Scan error:\s*/, "");
    }
    if (item.status === "rate_limited" || item.message.includes("under load")) {
      return item.message;
    }
    return item.message;
  }
  if (item.status === "rate_limited") return "Rate limited — reduce parallel scans and recheck";
  if (item.status === "needs_login") return "Not logged in — save your Battle.net session";
  return "Could not verify this code — recheck recommended";
}

function cardTitle(item) {
  const outcome = scanOutcome(item);
  if (outcome === "got_it") return item.name || outcomeLabel(outcome);
  return outcomeLabel(outcome);
}

function cardReason(item) {
  const reason = resultReason(item);
  if (!reason) return null;
  if (scanOutcome(item) === "got_it" && item.name) return reason;
  if (scanOutcome(item) === "got_it") return null;
  return reason;
}

function displayName(item) {
  if (item.name) return item.name;
  return cardTitle(item);
}

function displayPrice(item) {
  const price = item?.price;
  if (price === null || price === undefined) return "—";
  const trimmed = String(price).trim();
  if (!trimmed) return "—";
  return trimmed;
}

function displayGame(item) {
  if (item.game) return item.game;
  const genreMatch = (item.raw_notes || "").match(/Genre:\s*(.+)/i);
  if (genreMatch) return `Unknown (${genreMatch[1].trim()})`;
  return "Unknown game";
}

function cardClass(item) {
  const outcome = scanOutcome(item);
  if (outcome === "got_it") return "valid";
  if (outcome === "no_product") return "no-product";
  return "failed";
}

function gameBadge(item) {
  const game = displayGame(item);
  return `<span class="game">${escapeHtml(game)}</span>`;
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function loadStatus() {
  const data = await api("/api/status");
  applySettings(data.settings);
  updateStatus(data.scanner_status, data.logged_in);
  applyActivity(data);
  els.libraryStats.textContent = `${data.valid_count} valid / ${data.library_count} checked${data.incomplete_count ? ` · ${data.incomplete_count} incomplete` : ""}`;
}

function showScanPending(label) {
  els.currentResult.className = "current-result";
  els.currentResult.innerHTML = `<div class="empty-state">${escapeHtml(label)}</div>`;
}

function renderCurrentResult(item) {
  if (!item) return;
  const img = imageUrl(item);
  const outcome = scanOutcome(item);
  els.currentResult.className = `current-result ${cardClass(item)}`;
  els.currentResult.innerHTML = `
    <div class="code">Code ${item.code} · ${escapeHtml(outcomeLabel(outcome))}</div>
    ${outcome === "got_it" ? gameBadge(item) : ""}
    <div class="name">${escapeHtml(cardTitle(item))}</div>
    <div class="meta-row">
      <span>${escapeHtml(outcome === "got_it" ? displayPrice(item) : "")}</span>
    </div>
    ${cardReason(item) ? `<div class="result-detail">${escapeHtml(cardReason(item))}</div>` : ""}
    ${img ? `<img src="${img}" alt="${escapeHtml(cardTitle(item))}">` : ""}
    ${item.url ? `<div style="margin-top:0.5rem;font-size:0.8rem;"><a href="${item.url}" target="_blank" rel="noopener">Open checkout page</a></div>` : ""}
  `;
}

function renderRecentStrip(items) {
  if (!items.length) {
    els.recentStrip.innerHTML = `<div class="empty-state compact">No recent scans yet.</div>`;
    return;
  }

  els.recentStrip.innerHTML = items.map((item) => {
    const outcome = scanOutcome(item);
    return `
    <article class="recent-card ${cardClass(item)}">
      <div class="code">#${item.code} · ${escapeHtml(outcomeLabel(outcome))}</div>
      ${outcome === "got_it" ? `<div class="game">${escapeHtml(displayGame(item))}</div>` : ""}
      <div class="name">${escapeHtml(cardTitle(item))}</div>
      ${cardReason(item) ? `<div class="card-reason">${escapeHtml(cardReason(item))}</div>` : ""}
      ${outcome === "got_it" ? `<div class="price">${escapeHtml(displayPrice(item))}</div>` : ""}
    </article>
  `;
  }).join("");
}

function pushSessionRecent(item) {
  sessionRecent = [item, ...sessionRecent.filter((row) => row.code !== item.code)].slice(0, 5);
  renderRecentStrip(sessionRecent);
}

function measureVirtualLayout() {
  const width = els.libraryScroll.clientWidth - 4;
  VIRTUAL.cols = Math.max(1, Math.floor((width + VIRTUAL.gap) / (220 + VIRTUAL.gap)));
  VIRTUAL.rowHeight = VIRTUAL.cardHeight + VIRTUAL.gap;
}

function cardHtml(item) {
  const img = imageUrl(item);
  const thumb = img
    ? `<img src="${img}" alt="${escapeHtml(displayName(item))}" loading="lazy" decoding="async">`
    : `<div class="card-thumb-placeholder"></div>`;
  return `
    <article class="card ${cardClass(item)}">
      ${thumb}
      <div class="card-body">
        <div class="code">#${item.code} · ${escapeHtml(outcomeLabel(scanOutcome(item)))}</div>
        ${scanOutcome(item) === "got_it" ? gameBadge(item) : ""}
        <div class="name">${escapeHtml(cardTitle(item))}</div>
        ${cardReason(item) ? `<div class="card-reason">${escapeHtml(cardReason(item))}</div>` : ""}
        <div class="price">${escapeHtml(displayPrice(item))}</div>
        <div class="card-actions">
          ${item.url ? `<a class="card-open-link secondary" href="${item.url}" target="_blank" rel="noopener">Open</a>` : ""}
          <button type="button" class="danger" data-delete="${item.code}">Delete</button>
        </div>
      </div>
    </article>
  `;
}

function renderLibraryVirtual(force = false) {
  if (selectionInside(els.libraryGrid) && !force) return;

  if (!libraryTotal) {
    lastLibraryRenderKey = "";
    els.libraryVirtual.style.height = "auto";
    els.libraryGrid.style.transform = "";
    els.libraryGrid.innerHTML = `<div class="empty-state">Library is empty. Start scanning to build your collection.</div>`;
    return;
  }

  measureVirtualLayout();
  const totalRows = Math.ceil(libraryTotal / VIRTUAL.cols);
  const totalHeight = totalRows * VIRTUAL.rowHeight;
  const scrollTop = els.libraryScroll.scrollTop;
  const viewHeight = els.libraryScroll.clientHeight;
  const startRow = Math.max(0, Math.floor(scrollTop / VIRTUAL.rowHeight) - VIRTUAL.bufferRows);
  const endRow = Math.min(
    totalRows,
    Math.ceil((scrollTop + viewHeight) / VIRTUAL.rowHeight) + VIRTUAL.bufferRows,
  );
  const startIdx = startRow * VIRTUAL.cols;
  let endIdx = Math.min(libraryTotal, endRow * VIRTUAL.cols);
  if (endIdx <= startIdx) {
    endIdx = Math.min(libraryTotal, startIdx + VIRTUAL.cols * 4);
  }
  const offsetY = startRow * VIRTUAL.rowHeight;
  const renderKey = visibleRangeSignature(startIdx, endIdx);
  const missing = countMissingInRange(startIdx, endIdx);

  if (!force && renderKey === lastLibraryRenderKey && missing === 0) {
    return;
  }

  els.libraryVirtual.style.height = `${totalHeight}px`;
  els.libraryGrid.style.transform = `translateY(${offsetY}px)`;

  if (force || renderKey !== lastLibraryRenderKey) {
    els.libraryGrid.innerHTML = Array.from({ length: endIdx - startIdx }, (_, offset) => {
      const item = getLibraryItem(startIdx + offset);
      return item ? cardHtml(item) : cardSkeletonHtml();
    }).join("");
    lastLibraryRenderKey = renderKey;
  }

  if (missing === 0 || libraryLoading) return;

  libraryLoading = true;
  loadLibraryWindow(startIdx, endIdx)
    .then(() => {
      libraryLoading = false;
      if (countMissingInRange(startIdx, endIdx) > 0) {
        lastLibraryRenderKey = "";
        scheduleLibraryRender();
      } else if (visibleRangeSignature(startIdx, endIdx) !== lastLibraryRenderKey) {
        lastLibraryRenderKey = "";
        scheduleLibraryRender();
      }
    })
    .catch((error) => {
      libraryLoading = false;
      console.error(error);
    });
}

function scheduleLibraryRender() {
  if (libraryScrollRaf) return;
  libraryScrollRaf = requestAnimationFrame(() => {
    libraryScrollRaf = null;
    renderLibraryVirtual();
  });
}

async function refreshRecent() {
  if (selectionInside(els.recentStrip)) {
    pendingRecentRefresh = true;
    return;
  }
  if (scannerStatus === "running" && sessionRecent.length) {
    renderRecentStrip(sessionRecent);
    return;
  }
  const items = await api("/api/library/recent?limit=5");
  renderRecentStrip(items);
}

async function refreshLibrary() {
  const params = libraryQueryParams();
  libraryQueryKey = libraryParamsKey(params);
  libraryCache.clear();
  lastLibraryRenderKey = "";
  els.libraryScroll.scrollTop = 0;

  const status = await api("/api/status");
  let count = params.validOnly ? status.valid_count : status.library_count;
  try {
    const countRes = await api(`/api/library-count?${libraryApiQuery(params)}`);
    if (typeof countRes.count === "number") count = countRes.count;
  } catch (error) {
    console.warn("Library count fallback:", error.message);
  }

  libraryTotal = count;
  els.libraryStats.textContent = `${status.valid_count} valid / ${status.library_count} checked${status.incomplete_count ? ` · ${status.incomplete_count} incomplete` : ""}`;

  if (!libraryTotal) {
    scheduleLibraryRender();
    return;
  }

  await loadLibraryWindow(0, Math.min(libraryTotal, CHUNK_SIZE * 2));
  scheduleLibraryRender();
}

function scheduleLibraryStatsRefresh() {
  clearTimeout(libraryRefreshTimer);
  libraryRefreshTimer = setTimeout(async () => {
    const status = await api("/api/status");
    els.libraryStats.textContent = `${status.valid_count} valid / ${status.library_count} checked${status.incomplete_count ? ` · ${status.incomplete_count} incomplete` : ""}`;
  }, 400);
}

async function refreshAll() {
  await Promise.all([refreshRecent(), refreshLibrary()]);
}

async function saveSettings() {
  const body = {
    region: els.region.value,
    delay_ms: Number(els.delayMs.value),
    concurrency: Number(els.concurrency.value),
    start_code: Number(els.startCode.value),
    end_code: Number(els.endCode.value),
    current_code: Number(els.currentCode.value),
    headless: els.headless.checked,
    skip_scanned: els.skipScanned.checked,
    skip_no_product: els.skipNoProduct.checked,
  };
  await api("/api/settings", { method: "PUT", body: JSON.stringify(body) });
}

function connectWebSocket() {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${protocol}://${location.host}${apiUrl("/ws")}`);

  ws.onmessage = (event) => {
    const { event: name, payload } = JSON.parse(event.data);
    if (name === "connected") {
      applySettings(payload.settings);
      updateStatus(payload.scanner_status, payload.logged_in);
      applyActivity(payload);
      return;
    }
    if (name === "session_started") {
      sessionRecent = [];
      applyActivity(payload);
    }
    if (name === "scan_started" || name === "scan_finished" || name === "progress") {
      applyActivity(payload);
    }
    if (name === "scan_result") {
      pushSessionRecent(payload);
      if (selectionInside(els.currentResult)) {
        pendingCurrentResult = payload;
      } else {
        renderCurrentResult(payload);
      }
      scheduleLibraryStatsRefresh();
      if (payload.next_code) els.currentCode.value = payload.next_code;
    }
    if (name === "progress" && payload.next_code) {
      els.currentCode.value = payload.next_code;
    }
    if (name === "library_updated") scheduleLibraryStatsRefresh();
    if (name === "auto_started") {
      sessionRecent = [];
      updateStatus("running", true);
    }
    if (name === "auto_paused") updateStatus("paused", true);
    if (name === "auto_resumed") updateStatus("running", true);
    if (name === "auto_finished" || name === "auto_stopped") {
      updateStatus("idle", true);
      setBusy(false);
      refreshRecent();
    }
    if (name === "login_saved") loadStatus();
  };

  ws.onclose = () => setTimeout(connectWebSocket, 1500);
}

els.libraryScroll.addEventListener("scroll", scheduleLibraryRender, { passive: true });
window.addEventListener("resize", scheduleLibraryRender);

els.libraryGrid.addEventListener("click", async (event) => {
  const openLink = event.target.closest(".card-open-link");
  if (openLink) {
    event.stopPropagation();
    return;
  }
  const btn = event.target.closest("[data-delete]");
  if (!btn) return;
  event.preventDefault();
  const code = Number(btn.dataset.delete);
  await api(`/api/library/${code}`, { method: "DELETE" });
  lastLibraryRenderKey = "";
  await refreshAll();
});

els.saveSettingsBtn.addEventListener("click", saveSettings);

els.openLoginBtn.addEventListener("click", async () => {
  await api("/api/login/open", { method: "POST" });
  alert("A browser window opened. Log in to Battle.net, then click Save Login Session.");
});

els.saveLoginBtn.addEventListener("click", async () => {
  const res = await api("/api/login/save", { method: "POST" });
  if (!res.saved) {
    alert("Could not save session. Open the login browser first.");
    return;
  }
  alert("Login session saved.");
  await loadStatus();
});

els.nextBtn.addEventListener("click", async () => {
  setBusy(true);
  await saveSettings();
  showScanPending(`Scanning code ${els.currentCode.value}…`);
  try {
    const res = await api("/api/scan/next", { method: "POST" });
    if (res.result) renderCurrentResult(res.result);
    if (res.next_code) els.currentCode.value = res.next_code;
    if (res.done) alert("Reached end code.");
    await refreshRecent();
    scheduleLibraryStatsRefresh();
  } catch (error) {
    showScanPending(`Scan failed: ${error.message}`);
    alert(error.message);
  } finally {
    setBusy(false);
  }
});

els.scanOneBtn.addEventListener("click", async () => {
  const code = Number(els.scanOneCode.value || els.currentCode.value);
  if (!code) return;
  setBusy(true);
  showScanPending(`Scanning code ${code}…`);
  try {
    const res = await api("/api/scan/one", {
      method: "POST",
      body: JSON.stringify({
        code,
        region: els.region.value,
        headless: els.headless.checked,
      }),
    });
    renderCurrentResult(res.result);
    await refreshRecent();
    scheduleLibraryStatsRefresh();
  } catch (error) {
    showScanPending(`Scan failed: ${error.message}`);
    alert(error.message);
  } finally {
    setBusy(false);
  }
});

els.autoStartBtn.addEventListener("click", async () => {
  try {
    await saveSettings();
  } catch (error) {
    console.warn("Settings save failed:", error.message);
  }
  setBusy(true);
  try {
    const res = await api("/api/scan/auto/start", {
      method: "POST",
      body: JSON.stringify({
        start_code: Number(els.startCode.value),
        end_code: Number(els.endCode.value),
        delay_ms: Number(els.delayMs.value),
        concurrency: Number(els.concurrency.value),
        skip_scanned: els.skipScanned.checked,
    skip_no_product: els.skipNoProduct.checked,
        region: els.region.value,
        headless: els.headless.checked,
      }),
    });
    if (!res.ok) {
      alert(res.error || "Could not start auto scan.");
      setBusy(false);
      return;
    }
    updateStatus("running", true);
    showScanPending("Auto scan started…");
  } catch (error) {
    alert(error.message);
    setBusy(false);
  }
});

els.revalidateBtn.addEventListener("click", async () => {
  const start = Number(els.startCode.value);
  const end = Number(els.endCode.value);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) {
    alert("Set a valid start/end code range first.");
    return;
  }
  if (!confirm(`Recheck failed scans from ${start} to ${end}? This can be a large job if many codes failed.`)) {
    return;
  }
  setBusy(true);
  try {
    const res = await api("/api/library/revalidate", {
      method: "POST",
      body: JSON.stringify({
        start_code: start,
        end_code: end,
        delay_ms: Number(els.delayMs.value),
        concurrency: Math.min(Number(els.concurrency.value) || 4, 8),
        region: els.region.value,
        headless: els.headless.checked,
      }),
    });
    if (!res.ok) {
      alert(res.error || "Could not start recheck.");
      setBusy(false);
      return;
    }
    if (res.count === 0) {
      alert("No failed scans in that range to recheck.");
      setBusy(false);
      return;
    }
    updateStatus("running", true);
    showScanPending(`Rechecking ${res.count.toLocaleString()} failed scans…`);
  } catch (error) {
    alert(error.message);
    setBusy(false);
  }
});

els.enrichIncompleteBtn.addEventListener("click", async () => {
  setBusy(true);
  try {
    const status = await api("/api/status");
    const count = status.incomplete_count || 0;
    if (!count) {
      alert("No incomplete hits to fix — everything has price, image, and game.");
      setBusy(false);
      return;
    }
    if (!confirm(`Fix ${count.toLocaleString()} incomplete hits? Uses browser to fill in missing price, image, and game.`)) {
      setBusy(false);
      return;
    }
    const res = await api("/api/library/enrich-incomplete", {
      method: "POST",
      body: JSON.stringify({
        start_code: 1,
        end_code: 9999999,
        delay_ms: 0,
        concurrency: Math.min(Number(els.concurrency.value) || 3, 3),
        region: els.region.value,
        headless: els.headless.checked,
      }),
    });
    if (!res.ok) {
      alert(res.error || "Could not start fix.");
      setBusy(false);
      return;
    }
    updateStatus("running", true);
    showScanPending(`Fixing ${res.count.toLocaleString()} incomplete hits…`);
  } catch (error) {
    alert(error.message);
    setBusy(false);
  }
});

els.autoPauseBtn.addEventListener("click", () => api("/api/scan/auto/pause", { method: "POST" }));
els.autoResumeBtn.addEventListener("click", () => api("/api/scan/auto/resume", { method: "POST" }));
els.autoStopBtn.addEventListener("click", async () => {
  await api("/api/scan/auto/stop", { method: "POST" });
  setBusy(false);
});

els.searchInput.addEventListener("input", () => {
  clearTimeout(searchDebounceTimer);
  searchDebounceTimer = setTimeout(refreshLibrary, 250);
});
els.sortBy.addEventListener("change", refreshLibrary);
els.validOnly.addEventListener("change", refreshLibrary);

els.exportBtn.addEventListener("click", async () => {
  const data = await api("/api/library/export/json");
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "battlenet-library.json";
  a.click();
  URL.revokeObjectURL(url);
});

connectWebSocket();
loadStatus().then(refreshAll);
