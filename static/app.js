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
  smartScanBtn: document.getElementById("smartScanBtn"),
  fixProblemsBtn: document.getElementById("fixProblemsBtn"),
  fixThrottledBtn: document.getElementById("fixThrottledBtn"),
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
  failedOnly: document.getElementById("failedOnly"),
  incompleteOnly: document.getElementById("incompleteOnly"),
  notEligibleOnly: document.getElementById("notEligibleOnly"),
  exportBtn: document.getElementById("exportBtn"),
  refreshLibraryBtn: document.getElementById("refreshLibraryBtn"),
  deleteFromCode: document.getElementById("deleteFromCode"),
  deleteToCode: document.getElementById("deleteToCode"),
  deleteRangeBtn: document.getElementById("deleteRangeBtn"),
  debugDialog: document.getElementById("debugDialog"),
  debugDialogTitle: document.getElementById("debugDialogTitle"),
  debugDialogBody: document.getElementById("debugDialogBody"),
  debugDialogClose: document.getElementById("debugDialogClose"),
  libraryScroll: document.getElementById("libraryScroll"),
  libraryVirtual: document.getElementById("libraryVirtual"),
  libraryGrid: document.getElementById("libraryGrid"),
  libraryStats: document.getElementById("libraryStats"),
  debugLog: document.getElementById("debugLog"),
  debugBadge: document.getElementById("debugBadge"),
  debugPanel: document.getElementById("debugPanel"),
  staleServerBanner: document.getElementById("staleServerBanner"),
  databaseSummary: document.getElementById("databaseSummary"),
  databaseSelect: document.getElementById("databaseSelect"),
  databaseBackupSelect: document.getElementById("databaseBackupSelect"),
  databaseNewName: document.getElementById("databaseNewName"),
  databaseSwitchBtn: document.getElementById("databaseSwitchBtn"),
  databaseCreateBtn: document.getElementById("databaseCreateBtn"),
  databaseRestoreBtn: document.getElementById("databaseRestoreBtn"),
  dbActiveName: document.getElementById("dbActiveName"),
  dbValidCount: document.getElementById("dbValidCount"),
  dbRowCount: document.getElementById("dbRowCount"),
  dbProblemCount: document.getElementById("dbProblemCount"),
  problemsJsonPath: document.getElementById("problemsJsonPath"),
  refreshProblemsBtn: document.getElementById("refreshProblemsBtn"),
  activityLog: document.getElementById("activityLog"),
  activityLogStatus: document.getElementById("activityLogStatus"),
};

let ws;
let busy = false;
let scannerStatus = "idle";
let sessionStartedAt = null;
let activeScans = new Map();
let activeScanPhases = new Map();
let sessionCodesDone = 0;
let sessionCodesTotal = 0;
let etaSeconds = null;
let avgSecondsPerScan = null;
let scansPerSecond = null;
let enrichPending = 0;
let libraryTotal = 0;
let libraryCache = new Map();
let libraryQueryKey = "";
let libraryLoading = false;
let libraryRefreshTimer = null;
let libraryLiveSyncTimer = null;
let pendingLibraryLiveSync = false;
let libraryScrollRaf = null;
let searchDebounceTimer = null;
let lastLibraryRenderKey = "";
let pendingRecentRefresh = false;
let pendingCurrentResult = null;

const CHUNK_SIZE = 120;
const UI_SCANNER_VERSION = "2026-06-04.10";

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
  if (pendingLibraryLiveSync) {
    pendingLibraryLiveSync = false;
    scheduleLibraryLiveSync(null);
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
    failedOnly: els.failedOnly.checked,
    incompleteOnly: els.incompleteOnly.checked,
    notEligibleOnly: els.notEligibleOnly?.checked,
    search: els.searchInput.value.trim(),
    sort: els.sortBy.value,
  };
}

function libraryParamsKey(params) {
  return `${params.validOnly}|${params.failedOnly}|${params.incompleteOnly}|${params.notEligibleOnly}|${params.search}|${params.sort}`;
}

function libraryApiQuery(params, extra = {}) {
  const q = new URLSearchParams({
    valid_only: String(params.validOnly),
    failed_only: String(params.failedOnly),
    incomplete_only: String(params.incompleteOnly),
    not_eligible_only: String(Boolean(params.notEligibleOnly)),
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
    <article class="product-card library-card invalid">
      <div class="product-thumb card-thumb-placeholder"></div>
      <div class="product-body">
        <span class="product-code">Loading…</span>
      </div>
    </article>
  `;
}

const VIRTUAL = {
  cardHeight: 320,
  gap: 16,
  cols: 1,
  rowHeight: 316,
  bufferRows: 5,
};

function notifyParentUpdate() {
  if (window.parent === window) return;
  window.parent.postMessage({ type: "battlenet-scanner-update" }, "*");
}

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

let activeJobMode = null;

function phaseLabel(phase) {
  if (!phase) return "";
  const labels = {
    queued: "queued",
    http: "HTTP",
    browser: "browser",
  };
  return labels[phase] || phase;
}

function showFixProgress() {
  if (activeJobMode !== "fix_problems" && activeJobMode !== "fix_throttled" && activeJobMode !== "revalidate" && activeJobMode !== "enrich_incomplete") {
    return;
  }
  if (sessionCodesTotal <= 0) return;
  const label =
    activeJobMode === "fix_problems"
      ? "Fixing"
      : activeJobMode === "fix_throttled"
        ? "Retrying throttled"
        : activeJobMode === "revalidate"
          ? "Rechecking"
          : "Enriching";
  const activeNote = activeScans.size > 0 ? ` · ${activeScans.size} active` : "";
  const parallel = fixJobWorkers > 1 ? ` · ${fixJobWorkers} parallel` : "";
  showScanPending(`${label} ${sessionCodesDone.toLocaleString()} / ${sessionCodesTotal.toLocaleString()}${parallel}${activeNote}…`);
}

let fixJobWorkers = 1;

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
  if (data.active_scan_phases) {
    activeScanPhases = new Map(
      Object.entries(data.active_scan_phases).map(([code, phase]) => [Number(code), phase]),
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
  if (data.enrich_pending !== undefined) {
    enrichPending = data.enrich_pending;
  }
  updateSessionDisplay();
  showFixProgress();
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
      const queued = [...activeScanPhases.values()].filter((phase) => phase === "queued").length;
      setTextIfChanged(
        els.currentScanInfo,
        queued > 0 ? `${queued} queued…` : sessionCodesTotal > 0 && sessionCodesDone === 0 ? "Starting…" : "Between scans…",
      );
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
      phase: activeScanPhases.get(code) || "",
    }))
    .sort((a, b) => b.elapsed - a.elapsed);

  const longest = entries[0];
  const phaseText = phaseLabel(longest.phase);
  let scanText;
  if (entries.length === 1) {
    scanText = phaseText
      ? `#${longest.code} · ${phaseText} · ${formatDuration(longest.elapsed)}`
      : `#${longest.code} · ${formatDuration(longest.elapsed)}`;
  } else {
    const preview = entries
      .slice(0, 2)
      .map((entry) => {
        const phase = phaseLabel(entry.phase);
        return phase
          ? `#${entry.code} ${phase} (${formatDuration(entry.elapsed)})`
          : `#${entry.code} (${formatDuration(entry.elapsed)})`;
      })
      .join(", ");
    const suffix = entries.length > 2 ? ` +${entries.length - 2}` : "";
    scanText = `${entries.length} active: ${preview}${suffix}`;
  }
  if (enrichPending > 0) {
    scanText += ` · ${enrichPending} enriching`;
  }
  setTextIfChanged(els.currentScanInfo, scanText);
  setClassIfChanged(
    els.currentScanInfo,
    longest.elapsed >= 45000 ? "header-stat-value stuck" : "header-stat-value active",
  );
}

setInterval(updateSessionDisplay, 1000);
setInterval(() => {
  if (scannerStatus === "running" || scannerStatus === "paused") {
    void refreshActivityLog();
    api("/api/status")
      .then((data) => {
        updateStatus(data.scanner_status, data.logged_in);
        applyActivity(data);
        showFixProgress();
      })
      .catch(() => {});
  }
}, 2500);

function imageUrl(item) {
  if (!item?.image_path && !item?.image_url) return null;
  if (item.image_path) return assetUrl(item.image_path.replace(/\\/g, "/"));
  return item.image_url;
}

function recentThumbHtml(item) {
  const img = imageUrl(item);
  const remote = item?.image_url ? escapeHtml(item.image_url) : "";
  if (!img) {
    return `<div class="recent-card-thumb recent-card-thumb-empty"><span>No image</span></div>`;
  }
  return `<div class="recent-card-thumb"><img src="${escapeHtml(img)}"${
    remote && img !== remote ? ` data-fallback="${remote}"` : ""
  } alt="" loading="lazy" decoding="async" onerror="if(this.dataset.fallback){this.onerror=null;this.src=this.dataset.fallback}else{this.replaceWith(Object.assign(document.createElement('div'),{className:'recent-card-thumb recent-card-thumb-empty',innerHTML:'<span>No image</span>'}))}"></div>`;
}

const hydratePending = new Map();

async function hydrateScanResult(item) {
  if (!item?.code || scanOutcome(item) !== "got_it" || imageUrl(item)) return item;
  if (scannerStatus === "running" || scannerStatus === "paused") return item;
  if (hydratePending.has(item.code)) return hydratePending.get(item.code);
  const pending = api(`/api/library/${item.code}`)
    .then((full) => (full?.error ? item : { ...item, ...full }))
    .catch(() => item);
  hydratePending.set(item.code, pending);
  try {
    return await pending;
  } finally {
    hydratePending.delete(item.code);
  }
}

const CHECKOUT_HOSTS = {
  us: "us.checkout.battle.net",
  eu: "eu.checkout.battle.net",
  kr: "kr.checkout.battle.net",
  tw: "tw.checkout.battle.net",
};

function checkoutUrl(item) {
  if (!item?.code) return null;
  const region = (els.region?.value || "us").toLowerCase();
  const host = CHECKOUT_HOSTS[region] || CHECKOUT_HOSTS.us;
  return `https://${host}/shop/en/checkout/buy/${item.code}`;
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

function formatLibraryStats(data, shown) {
  const parts = [`${data.valid_count} valid · ${data.library_count} checked`];
  parts.push(`${data.incomplete_count ?? 0} incomplete`);
  parts.push(`${data.failed_count ?? 0} failed`);
  if (data.not_eligible_count) parts.push(`${data.not_eligible_count} not eligible`);
  if (data.throttled_count) parts.push(`${data.throttled_count} throttled`);
  let text = parts.join(" · ");
  if (typeof shown === "number") text = `${shown} shown · ${text}`;
  return text;
}

async function refreshLibraryStatsNow() {
  try {
    const status = await api("/api/status");
    els.libraryStats.textContent = formatLibraryStats(status);
  } catch (error) {
    console.warn("Stats refresh failed:", error.message);
  }
}

async function api(path, options = {}) {
  const res = await fetch(apiUrl(path), {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (res.status === 405) {
      throw new Error("Method not allowed — restart run.bat to load the latest scanner server.");
    }
    throw new Error(formatApiError(data, res.statusText) || `Request failed (${res.status})`);
  }
  return data;
}

function readCodeField(inputEl, fallbackEl) {
  const raw = inputEl.value.trim();
  if (raw !== "") return Number(raw);
  const fallback = fallbackEl.value.trim();
  return fallback === "" ? NaN : Number(fallback);
}

function setBusy(state) {
  busy = state;
  els.nextBtn.disabled = state;
  els.scanOneBtn.disabled = state;
  els.smartScanBtn.disabled = state;
  els.fixProblemsBtn.disabled = state;
  els.fixThrottledBtn.disabled = state;
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
  els.startCode.value = settings.start_code ?? 0;
  els.endCode.value = settings.end_code ?? 1000000;
  els.currentCode.value = settings.current_code ?? settings.start_code ?? 0;
  els.headless.checked = settings.headless !== false;
  els.skipScanned.checked = settings.skip_scanned !== false;
  els.skipNoProduct.checked = settings.skip_no_product === true;
}

let sessionRecent = [];

function isThrottledProduct(item) {
  if (item?.status === "throttled") return true;
  const notes = (item?.raw_notes || "").toLowerCase();
  const msg = (item?.message || "").toLowerCase();
  return notes.includes("retry later") && notes.includes("throttling");
}

function throttledLabel(item) {
  return (item?.message || "").trim() || "Retry later — Battle.net is throttling this code";
}

function scanOutcome(item) {
  if (isThrottledProduct(item)) return "throttled";
  if (isNotEligibleProduct(item) || item?.status === "not_eligible") return "not_eligible";
  if (isAlreadyOwnedProduct(item)) return "already_owned";
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
      not_eligible: "Not eligible",
      throttled: "Throttled",
      already_owned: "Already owned",
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
  if (outcome === "not_eligible") return item.name || notEligibleLabel(item);
  if (outcome === "throttled") return item.name || throttledLabel(item);
  if (outcome === "already_owned") return item.name || alreadyOwnedLabel(item);
  return outcomeLabel(outcome);
}

function cardReason(item) {
  const reason = resultReason(item);
  if (!reason) return null;
  const outcome = scanOutcome(item);
  if (outcome === "not_eligible") {
    return "Your account can't buy this — needs a different Battle.net account to fetch price/image.";
  }
  if (outcome === "throttled") {
    return "Battle.net is rate-limiting this code — wait before retrying; Fix mode will skip it.";
  }
  if (outcome === "got_it" && item.name) return reason;
  if (outcome === "got_it") return null;
  return reason;
}

function displayName(item) {
  if (item.name) return item.name;
  return cardTitle(item);
}

function isNotEligibleProduct(item) {
  const notes = (item?.raw_notes || "").toLowerCase();
  const msg = (item?.message || "").toLowerCase();
  return (
    notes.includes("scan account not eligible") ||
    msg.includes("not eligible to purchase") ||
    msg.includes("not eligible for this product")
  );
}

function notEligibleLabel(item) {
  return (item?.message || "").trim() || "Scan account not eligible for this product";
}

function isAlreadyOwnedProduct(item) {
  const notes = (item?.raw_notes || "").toLowerCase();
  const msg = (item?.message || "").toLowerCase();
  return (
    notes.includes("scan account already owns") ||
    msg.includes("already owns this product") ||
    msg.includes("already have access to this product")
  );
}

function alreadyOwnedLabel(item) {
  return (item?.message || "").trim() || "Scan account already owns this product";
}

function isPrerequisiteProduct(item) {
  const notes = (item?.raw_notes || "").toLowerCase();
  const msg = (item?.message || "").toLowerCase();
  const name = (item?.name || "").toLowerCase();
  if (notes.includes("prerequisite required")) return true;
  if (msg.includes("first things first") || /you need .+ to purchase/.test(msg)) return true;
  if (/reaper of souls|lord of destruction/.test(name) && !item?.price) return true;
  return false;
}

function prerequisiteLabel(item) {
  const msg = (item?.message || "").trim();
  if (msg && /you need .+ to purchase/i.test(msg)) return msg;
  const line = (item?.raw_notes || "")
    .split("\n")
    .map((row) => row.trim())
    .find((row) => /you need .+ to purchase/i.test(row));
  if (line) return line;
  const name = (item?.name || "").toLowerCase();
  if (/reaper of souls|diablo iii.*upgrade/.test(name)) return "You need Diablo III to purchase this product";
  if (/lord of destruction/.test(name)) return "You need Diablo II to purchase this product";
  return "Requires prerequisite game";
}

function priceLineHtml(item) {
  if (scanOutcome(item) !== "got_it") return "";
  const text = displayPrice(item);
  const cls = isPrerequisiteProduct(item)
    ? "price price-prerequisite"
    : isAlreadyOwnedProduct(item)
      ? "price price-already-owned"
      : isNotEligibleProduct(item)
        ? "price price-not-eligible"
        : text === "—" || text.startsWith("No price")
          ? "price price-missing"
          : "price";
  return `<div class="${cls}">${escapeHtml(text)}</div>`;
}

function displayPrice(item) {
  if (isPrerequisiteProduct(item)) return prerequisiteLabel(item);
  if (isAlreadyOwnedProduct(item)) return alreadyOwnedLabel(item);
  if (isNotEligibleProduct(item)) return notEligibleLabel(item);
  const price = item?.price;
  if (price === null || price === undefined) {
    return scanOutcome(item) === "got_it" ? "No price — Scan One" : "—";
  }
  const trimmed = String(price).trim();
  if (!trimmed) return scanOutcome(item) === "got_it" ? "No price — Scan One" : "—";
  return trimmed;
}

async function loadDebugEntries() {
  try {
    return await api("/api/debug/log?limit=80");
  } catch (error) {
    if (!String(error.message || "").includes("Not Found")) throw error;
  }

  try {
    const status = await api("/api/status");
    if (status.debug) {
      return {
        entries: status.debug.recent || [],
        ok: status.debug.ok,
        error: status.debug.error,
        path: status.debug.log_path,
        stale_server: true,
      };
    }
  } catch {
    /* status may also be stale */
  }

  const res = await fetch(apiUrl("/data/scanner-debug.log"));
  if (!res.ok) throw new Error("Not Found");
  const text = await res.text();
  const entries = text
    .trim()
    .split("\n")
    .filter(Boolean)
    .slice(-80)
    .map((line) => {
      try {
        return JSON.parse(line);
      } catch {
        return { ts: "", level: "info", event: "log_line", error: line };
      }
    });
  return {
    entries,
    path: "data/scanner-debug.log",
    stale_server: true,
  };
}

function renderActivityLog(entries, scriptStatus) {
  if (!els.activityLog) return;
  const statusText =
    scannerStatus === "running"
      ? `${activeScans.size} active · ${sessionCodesDone}/${sessionCodesTotal || "—"} done`
      : scannerStatus === "paused"
        ? "Paused"
        : "Idle";
  if (els.activityLogStatus) els.activityLogStatus.textContent = statusText;

  const lines = (entries || []).map(formatDebugEntry);
  if (activeScans.size > 0) {
    const activeLines = [...activeScans.entries()]
      .sort((a, b) => Number(a[0]) - Number(b[0]))
      .slice(0, 8)
      .map(([code, started]) => {
        const phase = phaseLabel(activeScanPhases.get(Number(code)));
        const elapsed = formatDuration(Date.now() - parseTime(started));
        return phase ? `ACTIVE | #${code} | ${phase} | ${elapsed}` : `ACTIVE | #${code} | ${elapsed}`;
      });
    lines.unshift(...activeLines);
  }
  if (scriptStatus?.ok === false) {
    lines.unshift(`EXTRACT SCRIPT BROKEN | ${scriptStatus.error || "see server console"}`);
  }
  els.activityLog.textContent = lines.length ? lines.join("\n") : "No activity yet — start a scan.";
}

async function refreshActivityLog() {
  if (!els.activityLog) return;
  try {
    const data = await loadDebugEntries();
    renderActivityLog(data.entries || [], data);
    if (els.debugLog) renderDebugLog(data.entries || [], data);
  } catch (error) {
    els.activityLog.textContent = `Activity log unavailable: ${error.message}`;
  }
}

async function refreshDebugLog() {
  await refreshActivityLog();
}

function displayGame(item) {
  if (item.game) return item.game;
  const name = item?.name || "";
  if (/starcraft[\s®™©\u00ae\u2122]*ii/i.test(name)) return "StarCraft II";
  if (/diablo[\s®™©\u00ae\u2122]*iii/i.test(name)) return "Diablo III";
  if (/diablo[\s®™©\u00ae\u2122]*iv/i.test(name)) return "Diablo IV";
  if (/diablo[\s®™©\u00ae\u2122]*ii/i.test(name)) return "Diablo II";
  if (/world of warcraft|\bwow\b/i.test(name)) return "World of Warcraft";
  if (/overwatch/i.test(name)) return "Overwatch";
  if (/hearthstone/i.test(name)) return "Hearthstone";
  const genreMatch = (item.raw_notes || "").match(/Genre:\s*(.+)/i);
  if (genreMatch) {
    const genre = genreMatch[1].trim().toLowerCase();
    if (genre.includes("real-time strategy")) return "StarCraft II";
    if (genre.includes("strategy card")) return "Hearthstone";
    if (genre.includes("massively multiplayer") || genre.includes("mmorpg")) return "World of Warcraft";
    if (genre.includes("first-person shooter") || genre.includes("first person shooter")) return "Overwatch";
    if (genre.includes("action rpg") && /diablo/i.test(name)) return "Diablo III";
    return `Unknown (${genreMatch[1].trim()})`;
  }
  return "Unknown game";
}

function cardClass(item) {
  const outcome = scanOutcome(item);
  if (outcome === "got_it") return "valid";
  if (outcome === "not_eligible") return "not-eligible";
  if (outcome === "throttled") return "throttled";
  if (outcome === "already_owned") return "already-owned";
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

function updateStaleBanner(status) {
  if (!els.staleServerBanner) return;
  const stale =
    !status?.scanner_version || status.scanner_version !== UI_SCANNER_VERSION;
  els.staleServerBanner.classList.toggle("hidden", !stale);
}

function formatDatabaseOption(db) {
  const mb = db.size_bytes ? `${(db.size_bytes / (1024 * 1024)).toFixed(1)} MB` : "0 MB";
  const active = db.active ? " · active" : "";
  return `${db.name} · ${db.valid_count ?? 0} valid · ${db.rows ?? 0} rows · ${mb}${active}`;
}

function renderDatabasePanel(payload) {
  if (!els.databaseSelect) return;
  const active = payload.active || payload.database?.name;
  const databases = payload.databases || [];
  const backups = payload.backups || [];

  els.databaseSelect.innerHTML = databases
    .map(
      (db) =>
        `<option value="${escapeHtml(db.name)}"${db.name === active ? " selected" : ""}>${escapeHtml(formatDatabaseOption(db))}</option>`,
    )
    .join("");

  els.databaseBackupSelect.innerHTML = backups.length
    ? backups
        .map(
          (snap) =>
            `<option value="${escapeHtml(snap.id)}">${escapeHtml(`${snap.id} · ${snap.valid_count} valid · ${snap.rows} rows`)}</option>`,
        )
        .join("")
    : `<option value="">No backups found</option>`;

  const current = databases.find((db) => db.name === active) || payload.database;
  if (current) {
    if (els.dbActiveName) els.dbActiveName.textContent = current.name;
    if (els.dbValidCount) els.dbValidCount.textContent = (current.valid_count ?? 0).toLocaleString();
    if (els.dbRowCount) els.dbRowCount.textContent = (current.rows ?? 0).toLocaleString();
    const problems = (current.failed_count ?? 0) + (current.incomplete_count ?? 0);
    if (els.dbProblemCount) els.dbProblemCount.textContent = problems.toLocaleString();
  }
  const problemsPath = payload.problems_json || payload.database?.problems_json;
  if (problemsPath && els.problemsJsonPath) {
    els.problemsJsonPath.textContent = problemsPath.replace(/^.*[/\\]data[/\\]/, "data/");
  }
}

async function refreshProblemsJson() {
  const data = await api("/api/library/problems?refresh=1");
  if (els.problemsJsonPath && data.path) {
    els.problemsJsonPath.textContent = data.path.replace(/^.*[/\\]data[/\\]/, "data/");
  }
  if (els.dbProblemCount && data.summary) {
    els.dbProblemCount.textContent = (data.summary.total_problems ?? 0).toLocaleString();
  }
  showScanPending(`Problems JSON updated — ${data.summary?.total_problems ?? 0} codes`);
  return data;
}

async function refreshDatabases() {
  if (!els.databaseSelect) return;
  const data = await api("/api/databases");
  renderDatabasePanel(data);
  return data;
}

async function switchDatabase(name) {
  const data = await api("/api/databases/switch", {
    method: "POST",
    body: JSON.stringify({ name }),
  });
  if (!data.ok) throw new Error(data.error || "Switch failed");
  await loadStatus();
  await refreshDatabases();
  await refreshAll();
  return data;
}

async function createDatabase(name) {
  const data = await api("/api/databases/create", {
    method: "POST",
    body: JSON.stringify({ name, switch: true }),
  });
  if (!data.ok) throw new Error(data.error || "Create failed");
  await loadStatus();
  await refreshDatabases();
  await refreshAll();
  return data;
}

async function restoreDatabase(backupId) {
  const data = await api("/api/databases/restore", {
    method: "POST",
    body: JSON.stringify({ backup_id: backupId, restore_settings: true }),
  });
  if (!data.ok) throw new Error(data.error || "Restore failed");
  await loadStatus();
  await refreshDatabases();
  await refreshAll();
  return data;
}

async function loadStatus() {
  const data = await api("/api/status");
  applySettings(data.settings);
  updateStatus(data.scanner_status, data.logged_in);
  applyActivity(data);
  if (data.problems_json && els.problemsJsonPath) {
    els.problemsJsonPath.textContent = data.problems_json.replace(/^.*[/\\]data[/\\]/, "data/");
  }
  if (data.database) {
    if (els.dbActiveName) els.dbActiveName.textContent = data.database.name;
    if (els.dbValidCount) els.dbValidCount.textContent = (data.valid_count ?? 0).toLocaleString();
    if (els.dbRowCount) els.dbRowCount.textContent = (data.library_count ?? 0).toLocaleString();
    const problems = (data.failed_count ?? 0) + (data.incomplete_count ?? 0);
    if (els.dbProblemCount) els.dbProblemCount.textContent = problems.toLocaleString();
  }
  updateStaleBanner(data);
  els.libraryStats.textContent = formatLibraryStats(data);
  return data;
}

function showScanPending(label) {
  els.currentResult.className = "current-result";
  els.currentResult.innerHTML = `<div class="empty-state">${escapeHtml(label)}</div>`;
}

function renderCurrentResult(item) {
  if (!item) return;
  const img = imageUrl(item);
  const outcome = scanOutcome(item);
  els.currentResult.className = `current-result is-fresh ${cardClass(item)}`;
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

function renderRecentStrip(items, highlightCode = null) {
  if (!items.length) {
    els.recentStrip.innerHTML = `<div class="empty-state compact">No recent scans yet.</div>`;
    return;
  }

  els.recentStrip.innerHTML = items.map((item) => {
    const outcome = scanOutcome(item);
    const isNew = highlightCode !== null && item.code === highlightCode;
    const img = imageUrl(item);
    const thumb =
      outcome === "got_it"
        ? recentThumbHtml(item)
        : "";
    return `
    <article class="recent-card ${cardClass(item)}${isNew ? " is-new" : ""}">
      ${thumb}
      <div class="recent-card-body">
        <div class="code">#${item.code} · ${escapeHtml(outcomeLabel(outcome))}</div>
        ${outcome === "got_it" ? `<div class="game">${escapeHtml(displayGame(item))}</div>` : ""}
        <div class="name">${escapeHtml(cardTitle(item))}</div>
        ${cardReason(item) ? `<div class="card-reason">${escapeHtml(cardReason(item))}</div>` : ""}
        ${priceLineHtml(item)}
        ${checkoutUrl(item) ? `<div class="recent-card-actions"><a class="card-open-link" href="${checkoutUrl(item)}" target="_blank" rel="noopener">Open</a></div>` : ""}
      </div>
    </article>
  `;
  }).join("");
}

function pushSessionRecent(item) {
  sessionRecent = [item, ...sessionRecent.filter((row) => row.code !== item.code)].slice(0, 5);
  renderRecentStrip(sessionRecent, item.code);
}

function measureVirtualLayout() {
  const width = els.libraryScroll.clientWidth - 4;
  VIRTUAL.cols = Math.max(1, Math.floor((width + VIRTUAL.gap) / (240 + VIRTUAL.gap)));
  VIRTUAL.rowHeight = VIRTUAL.cardHeight + VIRTUAL.gap;
}

function thumbMediaHtml(item) {
  const outcome = scanOutcome(item);
  if (outcome === "no_product") {
    return `<div class="card-media-empty"><span class="card-media-badge">No product</span></div>`;
  }
  const img = imageUrl(item);
  if (!img) {
    return `<div class="card-media-empty"><span class="card-media-badge">No image</span></div>`;
  }
  return `
    <img class="card-media-image" src="${img}" alt="${escapeHtml(displayName(item))}" loading="lazy" decoding="async">
    <div class="card-media-empty card-media-fallback" hidden><span class="card-media-badge">Image failed</span></div>
    <div class="product-thumb-gradient" aria-hidden></div>`;
}

function cardNeedsDebug(item) {
  if (!item) return false;
  if (scanOutcome(item) !== "got_it") return true;
  if (isPrerequisiteProduct(item) || isAlreadyOwnedProduct(item) || isNotEligibleProduct(item)) return true;
  const price = (item.price || "").trim();
  const img = item.image_path || item.image_url;
  const notes = (item.raw_notes || "").toLowerCase();
  if (!price || (!img && !notes.includes("image: none"))) return true;
  return false;
}

function renderDebugDialog(data) {
  if (!els.debugDialog || !els.debugDialogBody) return;
  els.debugDialogTitle.textContent = `#${data.code} — ${data.category || "debug"}`;
  const parts = [];
  if (data.reasons?.length) {
    parts.push("<ul>" + data.reasons.map((r) => `<li>${escapeHtml(r)}</li>`).join("") + "</ul>");
  }
  if (data.fixes?.length) {
    parts.push(`<p class="debug-fix-label">Try:</p><ul class="debug-fix-list">${data.fixes.map((r) => `<li>${escapeHtml(r)}</li>`).join("")}</ul>`);
  }
  if (data.log?.length) {
    parts.push(`<p class="debug-fix-label">Scanner log:</p><ul class="debug-log-list">${data.log.map((r) => `<li>${escapeHtml(r)}</li>`).join("")}</ul>`);
  }
  if (data.checked_at) {
    parts.push(`<p class="debug-meta">Last scan: ${escapeHtml(String(data.checked_at))}</p>`);
  }
  els.debugDialogBody.innerHTML = parts.join("") || "<p>No details.</p>";
  if (typeof els.debugDialog.showModal === "function") els.debugDialog.showModal();
}

async function openCardDebug(code) {
  try {
    const data = await api(`/api/library/${code}/debug`);
    if (data.error) throw new Error(data.error);
    renderDebugDialog(data);
  } catch (error) {
    renderDebugDialog({
      code,
      category: "error",
      reasons: [error.message || "Could not load debug info — restart run.bat."],
      fixes: ["Scan One on this code"],
    });
  }
}

function cardHtml(item) {
  const outcome = scanOutcome(item);
  const game = outcome === "got_it" ? displayGame(item) : outcomeLabel(outcome);
  const price = displayPrice(item);
  const showPrice = outcome === "got_it";

  return `
    <article class="product-card library-card ${cardClass(item)}">
      <div class="product-thumb">
        ${thumbMediaHtml(item)}
      </div>
      <div class="product-body">
        <div class="product-meta">
          <button type="button" class="product-code" data-copy-code="${item.code}">#${item.code}</button>
          <span class="product-game-label">${escapeHtml(game)}</span>
        </div>
        <h2 class="product-name">${escapeHtml(cardTitle(item))}</h2>
        ${cardReason(item) ? `<div class="card-reason">${escapeHtml(cardReason(item))}</div>` : ""}
        <div class="product-price">${showPrice ? (price ? escapeHtml(price) : '<span class="price-unavailable">Price unavailable</span>') : ""}</div>
        <div class="library-card-actions">
          ${cardNeedsDebug(item) ? `<button type="button" class="secondary card-debug-btn" data-debug="${item.code}">Debug</button>` : ""}
          ${checkoutUrl(item) ? `<a class="card-open-link" href="${checkoutUrl(item)}" target="_blank" rel="noopener">Open</a>` : ""}
          <button type="button" class="danger card-delete-btn" data-delete="${item.code}">Delete</button>
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
  els.libraryGrid.style.transform = `translate3d(0, ${offsetY}px, 0)`;

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
  if (libraryScrollRaf) cancelAnimationFrame(libraryScrollRaf);
  libraryScrollRaf = requestAnimationFrame(() => {
    libraryScrollRaf = requestAnimationFrame(() => {
      libraryScrollRaf = null;
      renderLibraryVirtual();
    });
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

async function refreshLibrary(options = {}) {
  const preserveScroll = options.preserveScroll ?? false;
  const params = libraryQueryParams();
  libraryQueryKey = libraryParamsKey(params);
  libraryCache.clear();
  lastLibraryRenderKey = "";
  const scrollTop = preserveScroll ? els.libraryScroll.scrollTop : 0;
  if (!preserveScroll) els.libraryScroll.scrollTop = 0;

  const [status, countRes] = await Promise.all([
    api("/api/status"),
    api(`/api/library-count?${libraryApiQuery(params)}`).catch(() => ({ count: null })),
  ]);
  const count =
    typeof countRes?.count === "number"
      ? countRes.count
      : params.validOnly || params.failedOnly || params.incompleteOnly || params.notEligibleOnly
        ? 0
        : (status.valid_count ?? status.library_count ?? 0);

  libraryTotal = count;
  els.libraryStats.textContent = formatLibraryStats(status, count);

  if (!libraryTotal) {
    scheduleLibraryRender();
    return;
  }

  if (preserveScroll) {
    measureVirtualLayout();
    const startRow = Math.max(0, Math.floor(scrollTop / VIRTUAL.rowHeight) - VIRTUAL.bufferRows);
    const viewRows = Math.ceil(els.libraryScroll.clientHeight / VIRTUAL.rowHeight) + VIRTUAL.bufferRows * 2;
    const startIdx = startRow * VIRTUAL.cols;
    const endIdx = Math.min(libraryTotal, startIdx + viewRows * VIRTUAL.cols + CHUNK_SIZE);
    els.libraryScroll.scrollTop = scrollTop;
    await loadLibraryWindow(startIdx, endIdx);
  } else {
    await loadLibraryWindow(0, Math.min(libraryTotal, CHUNK_SIZE * 2));
  }
  scheduleLibraryRender();
}

function scheduleLibraryStatsRefresh() {
  clearTimeout(libraryRefreshTimer);
  libraryRefreshTimer = setTimeout(async () => {
    const status = await api("/api/status");
    els.libraryStats.textContent = formatLibraryStats(status);
  }, 400);
}

function scheduleLibraryLiveSync(changedItem) {
  clearTimeout(libraryLiveSyncTimer);
  libraryLiveSyncTimer = setTimeout(() => void libraryLiveSync(changedItem), 450);
}

async function libraryLiveSync(changedItem) {
  if (selectionInside(els.libraryGrid)) {
    pendingLibraryLiveSync = true;
    return;
  }

  try {
    if (!changedItem) {
      const params = libraryQueryParams();
      const countRes = await api(`/api/library-count?${libraryApiQuery(params)}`);
      const newTotal = typeof countRes.count === "number" ? countRes.count : libraryTotal;
      if (newTotal === libraryTotal) {
        await refreshLibraryStatsNow();
        return;
      }
    }
    await Promise.all([refreshLibrary({ preserveScroll: true }), refreshRecent()]);
  } catch (error) {
    console.warn("Library live sync:", error);
  }
}

async function refreshAll() {
  await Promise.all([refreshRecent(), refreshLibrary()]);
  notifyParentUpdate();
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

function formatDebugEntry(entry) {
  const parts = [entry.ts, (entry.level || "info").toUpperCase(), entry.event];
  if (entry.code != null) parts.push(`#${entry.code}`);
  if (entry.error) parts.push(String(entry.error).slice(0, 240));
  if (entry.hint) parts.push(`→ ${entry.hint}`);
  else if (entry.message) parts.push(String(entry.message).slice(0, 120));
  return parts.join(" | ");
}

function renderDebugLog(entries, scriptStatus) {
  if (!els.debugLog) return;
  if (scriptStatus?.ok === false) {
    els.debugBadge.textContent = "EXTRACT SCRIPT BROKEN";
    els.debugBadge.classList.remove("hidden");
  } else if (entries.some((e) => e.level === "error" || e.level === "critical")) {
    els.debugBadge.textContent = "ERRORS";
    els.debugBadge.classList.remove("hidden");
  } else {
    els.debugBadge.classList.add("hidden");
  }
  if (!entries.length) {
    els.debugLog.textContent = scriptStatus?.ok === false
      ? `EXTRACT_SCRIPT broken: ${scriptStatus.error || "see server console"}`
      : "(no entries yet)";
    return;
  }
  els.debugLog.textContent = entries.map(formatDebugEntry).join("\n");
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
      updateStaleBanner(payload);
      void refreshDebugLog();
      return;
    }
    if (name === "session_started") {
      sessionRecent = [];
      if (payload.workers) fixJobWorkers = payload.workers;
      applyActivity(payload);
      showFixProgress();
    }
    if (name === "scan_started" || name === "scan_finished" || name === "progress" || name === "scan_phase") {
      applyActivity(payload);
      if (name === "scan_phase" || name === "scan_started") void refreshActivityLog();
    }
    if (name === "scan_debug") {
      void refreshDebugLog();
      if (payload?.level === "critical" || payload?.level === "error") {
        showScanPending(payload.hint || payload.error || "Scan debug error — open Debug log");
      }
      return;
    }
    if (name === "scan_result") {
      void hydrateScanResult(payload).then((row) => {
        pushSessionRecent(row);
        if (selectionInside(els.currentResult)) {
          pendingCurrentResult = row;
        } else {
          renderCurrentResult(row);
        }
        scheduleLibraryLiveSync(row);
      });
      void refreshLibraryStatsNow();
      scheduleLibraryStatsRefresh();
      if (payload.next_code) els.currentCode.value = payload.next_code;
      if (payload.status === "error" || (payload.message || "").startsWith("Scan error:")) {
        void refreshDebugLog();
      }
    }
    if (name === "progress" && payload.next_code) {
      els.currentCode.value = payload.next_code;
    }
    if (name === "library_updated") {
      scheduleLibraryStatsRefresh();
      scheduleLibraryLiveSync(payload);
      notifyParentUpdate();
    }
    if (name === "auto_started") {
      sessionRecent = [];
      activeJobMode = payload.mode || null;
      if (payload.count) sessionCodesTotal = payload.count;
      sessionCodesDone = 0;
      fixJobWorkers = Math.min(Number(payload.concurrency) || 1, 6);
      updateStatus("running", true);
      void refreshActivityLog();
      showFixProgress();
    }
    if (name === "auto_paused") updateStatus("paused", true);
    if (name === "auto_resumed") updateStatus("running", true);
    if (name === "auto_finished" || name === "auto_stopped") {
      updateStatus("idle", true);
      activeJobMode = null;
      sessionCodesTotal = 0;
      sessionCodesDone = 0;
      setBusy(false);
      if (payload.error) {
        showScanPending(`Scan stopped: ${payload.error}`);
      }
      refreshRecent();
      void refreshActivityLog();
      void refreshDatabases();
    }
    if (name === "login_saved") loadStatus();
  };

  ws.onclose = () => setTimeout(connectWebSocket, 1500);
}

els.libraryScroll.addEventListener("scroll", scheduleLibraryRender, { passive: true });
window.addEventListener("resize", scheduleLibraryRender);

els.libraryGrid.addEventListener(
  "error",
  (event) => {
    const img = event.target;
    if (!(img instanceof HTMLImageElement) || !img.classList.contains("card-media-image")) return;
    img.hidden = true;
    const fallback = img.parentElement?.querySelector(".card-media-fallback");
    if (fallback) fallback.hidden = false;
  },
  true,
);

els.libraryGrid.addEventListener("click", async (event) => {
  const copyBtn = event.target.closest("[data-copy-code]");
  if (copyBtn) {
    event.preventDefault();
    event.stopPropagation();
    const code = copyBtn.dataset.copyCode;
    try {
      await navigator.clipboard.writeText(code);
      const prev = copyBtn.textContent;
      copyBtn.textContent = "Copied!";
      copyBtn.classList.add("is-copied");
      window.setTimeout(() => {
        copyBtn.textContent = prev;
        copyBtn.classList.remove("is-copied");
      }, 1400);
    } catch {
      /* clipboard unavailable */
    }
    return;
  }
  const openLink = event.target.closest(".card-open-link");
  if (openLink) {
    event.stopPropagation();
    return;
  }
  const debugBtn = event.target.closest("[data-debug]");
  if (debugBtn) {
    event.preventDefault();
    event.stopPropagation();
    await openCardDebug(Number(debugBtn.dataset.debug));
    return;
  }
  const btn = event.target.closest("[data-delete]");
  if (!btn) return;
  event.preventDefault();
  const code = Number(btn.dataset.delete);
  if (!window.confirm(`Delete #${code.toLocaleString()} from the library?`)) return;

  btn.disabled = true;
  try {
    await api(`/api/library/${code}`, { method: "DELETE" });
    lastLibraryRenderKey = "";
    libraryCache.clear();
    await refreshAll();
    if (scannerStatus === "running") {
      showScanPending(
        `Deleted #${code.toLocaleString()}. Auto-scan may add it back if that code is still in range.`,
      );
    } else {
      showScanPending(`Deleted #${code.toLocaleString()} from the library.`);
    }
  } catch (error) {
    showScanPending(`Delete failed: ${error.message}`);
  } finally {
    btn.disabled = false;
  }
});

els.saveSettingsBtn.addEventListener("click", saveSettings);

els.region.addEventListener("change", () => {
  saveSettings().catch((error) => console.warn("Settings save failed:", error.message));
});

els.openLoginBtn.addEventListener("click", async () => {
  try {
    const res = await api("/api/login/open", { method: "POST" });
    if (!res.ok) {
      showScanPending(res.error || "Could not open login browser.");
      return;
    }
    const browser = res.browser === "chrome" ? "Chrome" : res.browser === "msedge" ? "Edge" : "browser";
    const hint = res.warning || `A ${browser} window opened — log in to Battle.net (use Battle.net login, not Google if possible), then click Save Session.`;
    showScanPending(hint);
  } catch (error) {
    showScanPending(`Open login failed: ${error.message}`);
  }
});

els.saveLoginBtn.addEventListener("click", async () => {
  const res = await api("/api/login/save", { method: "POST" });
  if (!res.saved) {
    showScanPending("Could not save session. Open the login browser first.");
    return;
  }
  showScanPending("Login session saved.");
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
    if (res.done) showScanPending("Reached end code.");
    await refreshRecent();
    scheduleLibraryStatsRefresh();
  } catch (error) {
    showScanPending(`Scan failed: ${error.message}`);
  } finally {
    setBusy(false);
  }
});

els.scanOneBtn.addEventListener("click", async () => {
  const code = Number(els.scanOneCode.value || els.currentCode.value);
  if (!code) return;
  try {
    await saveSettings();
  } catch (error) {
    console.warn("Settings save failed:", error.message);
  }
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
  } finally {
    setBusy(false);
  }
});

els.smartScanBtn.addEventListener("click", async () => {
  const start = Number(els.currentCode.value);
  const end = Number(els.endCode.value);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) {
    showScanPending("Set Current and End codes first (End must be ≥ Current).");
    return;
  }
  try {
    await saveSettings();
  } catch (error) {
    console.warn("Settings save failed:", error.message);
  }
  setBusy(true);
  try {
    const res = await api("/api/scan/smart/start", {
      method: "POST",
      body: JSON.stringify({
        start_code: start,
        end_code: end,
        delay_ms: Number(els.delayMs.value),
        concurrency: Number(els.concurrency.value),
        region: els.region.value,
        headless: els.headless.checked,
      }),
    });
    if (!res.ok) {
      showScanPending(res.error || "Could not start scan.");
      setBusy(false);
      return;
    }
    updateStatus("running", true);
    const skipped = res.skipped ? ` · ${res.skipped.toLocaleString()} skipped` : "";
    showScanPending(`Scan started #${start.toLocaleString()} → #${end.toLocaleString()}${skipped}`);
    setBusy(false);
  } catch (error) {
    showScanPending(error.message);
    setBusy(false);
  }
});

els.fixProblemsBtn.addEventListener("click", async () => {
  const start = Number(els.startCode.value);
  const end = Number(els.endCode.value);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) {
    showScanPending("Set Start and End codes first.");
    return;
  }
  try {
    await saveSettings();
  } catch (error) {
    console.warn("Settings save failed:", error.message);
  }
  setBusy(true);
  try {
    const status = await api("/api/status");
    const failed = status.failed_count || 0;
    const incomplete = status.incomplete_count || 0;
    if (!failed && !incomplete) {
      showScanPending("Nothing to fix — no failed or incomplete hits.");
      setBusy(false);
      return;
    }
    const res = await api("/api/library/fix-problems", {
      method: "POST",
      body: JSON.stringify({
        start_code: start,
        end_code: end,
        delay_ms: Math.max(Number(els.delayMs.value) || 0, 400),
        concurrency: 1,
        region: els.region.value,
        headless: els.headless.checked,
      }),
    });
    if (!res.ok) {
      showScanPending(res.error || "Could not start fix.");
      setBusy(false);
      return;
    }
    if (res.count === 0) {
      showScanPending("Nothing to fix in that Start–End range.");
      setBusy(false);
      return;
    }
    const parts = [];
    if (res.failed_count) parts.push(`${res.failed_count.toLocaleString()} failed`);
    if (res.incomplete_count) parts.push(`${res.incomplete_count.toLocaleString()} incomplete`);
    showScanPending(`Fixing 0 / ${res.count.toLocaleString()} (${parts.join(", ")}) — starting…`);
    setBusy(false);
  } catch (error) {
    showScanPending(error.message);
    setBusy(false);
  }
});

els.fixThrottledBtn.addEventListener("click", async () => {
  const start = Number(els.startCode.value);
  const end = Number(els.endCode.value);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) {
    showScanPending("Set Start and End codes first.");
    return;
  }
  try {
    await saveSettings();
  } catch (error) {
    console.warn("Settings save failed:", error.message);
  }
  setBusy(true);
  try {
    const status = await api("/api/status");
    const throttled = status.throttled_count || 0;
    if (!throttled) {
      showScanPending("Nothing throttled in the library.");
      setBusy(false);
      return;
    }
    const res = await api("/api/library/fix-throttled", {
      method: "POST",
      body: JSON.stringify({
        start_code: start,
        end_code: end,
        delay_ms: Math.max(Number(els.delayMs.value) || 0, 400),
        concurrency: 1,
        region: els.region.value,
        headless: els.headless.checked,
      }),
    });
    if (!res.ok) {
      showScanPending(res.error || "Could not start throttled fix.");
      setBusy(false);
      return;
    }
    if (res.count === 0) {
      showScanPending("Nothing throttled in that Start–End range.");
      setBusy(false);
      return;
    }
    showScanPending(
      `Retrying throttled 0 / ${res.count.toLocaleString()} (${res.throttled_count.toLocaleString()} in range) — starting…`,
    );
    setBusy(false);
  } catch (error) {
    showScanPending(error.message);
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
      showScanPending(res.error || "Could not start auto scan.");
      setBusy(false);
      return;
    }
    updateStatus("running", true);
    showScanPending("Auto scan started…");
  } catch (error) {
    showScanPending(error.message);
    setBusy(false);
  }
});

els.revalidateBtn.addEventListener("click", async () => {
  const start = Number(els.startCode.value);
  const end = Number(els.endCode.value);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) {
    showScanPending("Set a valid start/end code range first.");
    return;
  }
  try {
    await saveSettings();
  } catch (error) {
    console.warn("Settings save failed:", error.message);
  }
  setBusy(true);
  try {
    const res = await api("/api/library/revalidate", {
      method: "POST",
      body: JSON.stringify({
        start_code: start,
        end_code: end,
        delay_ms: Math.max(Number(els.delayMs.value) || 0, 400),
        concurrency: 1,
        region: els.region.value,
        headless: els.headless.checked,
      }),
    });
    if (!res.ok) {
      showScanPending(res.error || "Could not start recheck.");
      setBusy(false);
      return;
    }
    if (res.count === 0) {
      showScanPending("No failed scans in that range to recheck.");
      setBusy(false);
      return;
    }
    updateStatus("running", true);
    showScanPending(`Rechecking 0 / ${res.count.toLocaleString()} failed…`);
    setBusy(false);
  } catch (error) {
    showScanPending(error.message);
    setBusy(false);
  }
});

els.enrichIncompleteBtn.addEventListener("click", async () => {
  try {
    await saveSettings();
  } catch (error) {
    console.warn("Settings save failed:", error.message);
  }
  setBusy(true);
  try {
    const status = await api("/api/status");
    const count = status.incomplete_count || 0;
    if (!count) {
      showScanPending("No incomplete hits to fix — everything has name, price, and image (or confirmed no image).");
      setBusy(false);
      return;
    }
    const res = await api("/api/library/enrich-incomplete", {
      method: "POST",
      body: JSON.stringify({
        start_code: 1,
        end_code: 9999999,
        delay_ms: 0,
        concurrency: Number(els.concurrency.value) || 3,
        region: els.region.value,
        headless: els.headless.checked,
      }),
    });
    if (!res.ok) {
      showScanPending(res.error || "Could not start fix.");
      setBusy(false);
      return;
    }
    updateStatus("running", true);
    showScanPending(`Fixing ${res.count.toLocaleString()} incomplete hits…`);
  } catch (error) {
    showScanPending(error.message);
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
els.validOnly.addEventListener("change", () => {
  refreshLibrary();
});
els.failedOnly.addEventListener("change", () => {
  refreshLibrary();
});
els.incompleteOnly.addEventListener("change", () => {
  refreshLibrary();
});
if (els.notEligibleOnly) {
  els.notEligibleOnly.addEventListener("change", () => {
    refreshLibrary();
  });
}

if (els.debugDialogClose) {
  els.debugDialogClose.addEventListener("click", () => els.debugDialog?.close());
}
if (els.debugDialog) {
  els.debugDialog.addEventListener("click", (event) => {
    if (event.target === els.debugDialog) els.debugDialog.close();
  });
}

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

els.refreshLibraryBtn.addEventListener("click", async () => {
  els.refreshLibraryBtn.disabled = true;
  els.refreshLibraryBtn.classList.add("is-refreshing");
  try {
    await refreshAll();
  } catch (error) {
    showScanPending(`Library refresh failed: ${error.message}`);
  } finally {
    els.refreshLibraryBtn.disabled = false;
    els.refreshLibraryBtn.classList.remove("is-refreshing");
  }
});

els.deleteRangeBtn.addEventListener("click", async () => {
  const from = readCodeField(els.deleteFromCode, els.startCode);
  const to = readCodeField(els.deleteToCode, els.endCode);
  if (!Number.isFinite(from) || !Number.isFinite(to)) {
    showScanPending("Enter a from/to code or set Start/End in Settings.");
    return;
  }
  const start = Math.min(from, to);
  const end = Math.max(from, to);
  const ok = window.confirm(
    `Delete all library entries from #${start.toLocaleString()} to #${end.toLocaleString()}? This cannot be undone.`,
  );
  if (!ok) return;

  els.deleteRangeBtn.disabled = true;
  try {
    const res = await api(
      `/api/library-range?start_code=${encodeURIComponent(start)}&end_code=${encodeURIComponent(end)}`,
      { method: "DELETE" },
    );
    if (!res.ok && res.error) {
      showScanPending(res.error);
      return;
    }
    const deleted = res.deleted ?? 0;
    showScanPending(
      deleted
        ? `Deleted ${deleted.toLocaleString()} entries (#${start.toLocaleString()}–#${end.toLocaleString()}).`
        : `No entries found in #${start.toLocaleString()}–#${end.toLocaleString()}.`,
    );
    await refreshAll();
  } catch (error) {
    showScanPending(`Delete failed: ${error.message}`);
  } finally {
    els.deleteRangeBtn.disabled = false;
  }
});

if (els.databaseSwitchBtn) {
  els.databaseSwitchBtn.addEventListener("click", async () => {
    const name = els.databaseSelect.value;
    if (!name) return;
    els.databaseSwitchBtn.disabled = true;
    try {
      await switchDatabase(name);
      showScanPending(`Switched to database “${name}”.`);
    } catch (error) {
      showScanPending(`Database switch failed: ${error.message}`);
    } finally {
      els.databaseSwitchBtn.disabled = false;
    }
  });
}

if (els.databaseCreateBtn) {
  els.databaseCreateBtn.addEventListener("click", async () => {
    const name = els.databaseNewName.value.trim();
    if (!name) {
      showScanPending("Enter a name for the new database.");
      return;
    }
    els.databaseCreateBtn.disabled = true;
    try {
      await createDatabase(name);
      els.databaseNewName.value = "";
      showScanPending(`Created and switched to “${name}”.`);
    } catch (error) {
      showScanPending(`Create database failed: ${error.message}`);
    } finally {
      els.databaseCreateBtn.disabled = false;
    }
  });
}

if (els.databaseRestoreBtn) {
  els.databaseRestoreBtn.addEventListener("click", async () => {
    const backupId = els.databaseBackupSelect.value;
    if (!backupId) {
      showScanPending("Choose a backup snapshot to restore.");
      return;
    }
    const ok = window.confirm(
      `Restore backup “${backupId}” into a new database and switch to it? Current scan settings from that backup will also be restored.`,
    );
    if (!ok) return;
    els.databaseRestoreBtn.disabled = true;
    try {
      const result = await restoreDatabase(backupId);
      showScanPending(`Restored backup into “${result.database.name}”.`);
    } catch (error) {
      showScanPending(`Restore failed: ${error.message}`);
    } finally {
      els.databaseRestoreBtn.disabled = false;
    }
  });
}

if (els.refreshProblemsBtn) {
  els.refreshProblemsBtn.addEventListener("click", async () => {
    els.refreshProblemsBtn.disabled = true;
    try {
      await refreshProblemsJson();
    } catch (error) {
      showScanPending(`Problems export failed: ${error.message}`);
    } finally {
      els.refreshProblemsBtn.disabled = false;
    }
  });
}

connectWebSocket();
if (els.debugPanel) {
  els.debugPanel.addEventListener("toggle", () => {
    if (els.debugPanel.open) void refreshDebugLog();
  });
}
loadStatus().then(() => Promise.all([refreshDatabases(), refreshAll()]));
