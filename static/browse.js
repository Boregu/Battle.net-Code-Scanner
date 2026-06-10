const els = {
  searchInput: document.getElementById("searchInput"),
  sortBy: document.getElementById("sortBy"),
  statsRow: document.getElementById("statsRow"),
  gameFilters: document.getElementById("gameFilters"),
  productGrid: document.getElementById("productGrid"),
  loadMoreSentinel: document.getElementById("loadMoreSentinel"),
  emptyState: document.getElementById("emptyState"),
  loadingState: document.getElementById("loadingState"),
  productModal: document.getElementById("productModal"),
  modalClose: document.getElementById("modalClose"),
  modalContent: document.getElementById("modalContent"),
};

const PAGE_SIZE = 48;
let total = 0;
let offset = 0;
let loading = false;
let done = false;
let activeGame = "";
let searchTimer = null;
let observer = null;

const GAME_COLORS = {
  "Call of Duty": "#e85d04",
  "World of Warcraft": "#0070dd",
  Overwatch: "#f99e1a",
  Hearthstone: "#ffb100",
  "Diablo IV": "#8b0000",
  "Diablo III": "#8b0000",
  "StarCraft II": "#00aeff",
};

function imageUrl(item) {
  if (item?.image_path) return assetUrl(item.image_path.replace(/\\/g, "/"));
  if (item?.image_url) return item.image_url;
  return null;
}

function displayPrice(item) {
  const price = item?.price;
  if (price === null || price === undefined) return null;
  const trimmed = String(price).trim();
  return trimmed || null;
}

function gameClass(game) {
  return (game || "unknown").toLowerCase().replace(/[^a-z0-9]+/g, "-");
}

function checkoutLink(item) {
  if (item?.url) return item.url;
  return `https://us.checkout.battle.net/shop/en/checkout/buy/${item.code}`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function cardHtml(item) {
  const img = imageUrl(item);
  const price = displayPrice(item);
  const game = item.game || "Unknown game";

  return `
    <article class="product-card" data-code="${item.code}" tabindex="0">
      <div class="product-thumb">
        ${
          img
            ? `<div class="thumb-shimmer" aria-hidden></div><img src="${escapeHtml(img)}" alt="" loading="lazy" class="is-loading" onload="this.classList.remove('is-loading');this.previousElementSibling?.remove()" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'thumb-fallback'}))">`
            : `<div class="thumb-fallback"><span>${escapeHtml(game.split(" ")[0])}</span></div>`
        }
      </div>
      <div class="product-body">
        <div class="product-meta">
          <span class="product-code">#${item.code}</span>
          <span class="product-game-label">${escapeHtml(game)}</span>
        </div>
        <h2 class="product-name">${escapeHtml(item.name || "Unnamed product")}</h2>
        <div class="product-price">${price ? escapeHtml(price) : '<span class="muted">Price unavailable</span>'}</div>
      </div>
    </article>
  `;
}

function staggerCards(startIndex = 0) {
  const cards = els.productGrid.querySelectorAll(".product-card:not(.card-enter)");
  cards.forEach((card, i) => {
    card.classList.add("card-enter");
    card.style.setProperty("--stagger", String(startIndex + i));
  });
}

function closeModal() {
  if (!els.productModal.open) return;
  els.productModal.classList.add("is-closing");
  window.setTimeout(() => {
    els.productModal.close();
    els.productModal.classList.remove("is-closing");
  }, 180);
}

function modalHtml(item) {
  const img = imageUrl(item);
  const price = displayPrice(item);
  const game = item.game || "Unknown game";

  return `
    <div class="modal-grid">
      <div class="modal-media">
        ${
          img
            ? `<img src="${escapeHtml(img)}" alt="">`
            : `<div class="thumb-fallback large"><span>${escapeHtml(game)}</span></div>`
        }
      </div>
      <div class="modal-details">
        <p class="modal-code">Code #${item.code}</p>
        <h2>${escapeHtml(item.name || "Unnamed product")}</h2>
        <p class="modal-game">${escapeHtml(game)}</p>
        <p class="modal-price">${price ? escapeHtml(price) : "Price not indexed yet"}</p>
        <a class="btn-primary" href="${escapeHtml(checkoutLink(item))}" target="_blank" rel="noopener">Open on Battle.net</a>
      </div>
    </div>
  `;
}

async function fetchJson(path) {
  const res = await fetch(apiUrl(path));
  if (!res.ok) throw new Error(`Request failed (${res.status})`);
  return res.json();
}

async function loadStats() {
  try {
    const stats = await fetchJson("/api/public/stats");
    total = stats.products || 0;
    els.statsRow.textContent = `${total.toLocaleString()} products indexed`;
    renderGameFilters(stats.games || []);
  } catch {
    els.statsRow.textContent = "Catalog unavailable";
  }
}

function renderGameFilters(games) {
  if (!games.length) {
    els.gameFilters.innerHTML = "";
    return;
  }

  const chips = [
    `<button type="button" class="game-chip${activeGame ? "" : " active"}" data-game="">All</button>`,
    ...games.slice(0, 12).map(
      (entry) =>
        `<button type="button" class="game-chip${activeGame === entry.game ? " active" : ""}" data-game="${escapeHtml(entry.game)}">${escapeHtml(entry.game)} <span>${entry.count}</span></button>`,
    ),
  ];
  els.gameFilters.innerHTML = chips.join("");

  els.gameFilters.querySelectorAll(".game-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      activeGame = chip.dataset.game || "";
      resetAndLoad();
    });
  });
}

function buildQuery() {
  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
    offset: String(offset),
    sort: els.sortBy.value,
    search: els.searchInput.value.trim(),
  });
  if (activeGame) params.set("game", activeGame);
  return params.toString();
}

async function loadMore() {
  if (loading || done) return;
  loading = true;
  els.loadingState.classList.remove("hidden");

  const prevCount = offset;

  try {
    const data = await fetchJson(`/api/public/library?${buildQuery()}`);
    total = data.total ?? total;
    const items = data.items || [];

    if (offset === 0) {
      els.productGrid.classList.add("is-refreshing");
      els.productGrid.innerHTML = "";
      requestAnimationFrame(() => {
        els.productGrid.classList.remove("is-refreshing");
      });
    }

    if (!items.length && offset === 0) {
      els.emptyState.classList.remove("hidden");
    } else {
      els.emptyState.classList.add("hidden");
      els.productGrid.insertAdjacentHTML("beforeend", items.map(cardHtml).join(""));
      staggerCards(prevCount);
    }

    offset += items.length;
    done = offset >= total || items.length < PAGE_SIZE;
    els.statsRow.textContent = `${total.toLocaleString()} products${els.searchInput.value.trim() || activeGame ? " matching" : " indexed"}`;
  } catch {
    if (offset === 0) {
      els.productGrid.innerHTML = "";
      els.emptyState.textContent = "Could not load catalog.";
      els.emptyState.classList.remove("hidden");
    }
  } finally {
    loading = false;
    els.loadingState.classList.add("hidden");
  }
}

function resetAndLoad() {
  offset = 0;
  done = false;
  els.emptyState.classList.add("hidden");
  els.emptyState.textContent = "No products match your search.";
  loadMore();
  renderGameFiltersFromActive();
}

function renderGameFiltersFromActive() {
  fetchJson("/api/public/stats")
    .then((stats) => renderGameFilters(stats.games || []))
    .catch(() => {});
}

function openModal(code) {
  fetchJson(`/api/library/${code}`)
    .then((item) => {
      if (item.error) return;
      els.modalContent.innerHTML = modalHtml(item);
      els.productModal.showModal();
    })
    .catch(() => {});
}

function setupInfiniteScroll() {
  observer = new IntersectionObserver(
    (entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        loadMore();
      }
    },
    { rootMargin: "400px" },
  );
  observer.observe(els.loadMoreSentinel);
}

els.searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(resetAndLoad, 250);
});

els.sortBy.addEventListener("change", resetAndLoad);

els.productGrid.addEventListener("click", (event) => {
  const card = event.target.closest(".product-card");
  if (!card) return;
  openModal(Number(card.dataset.code));
});

els.productGrid.addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  const card = event.target.closest(".product-card");
  if (!card) return;
  openModal(Number(card.dataset.code));
});

els.modalClose.addEventListener("click", closeModal);
els.productModal.addEventListener("click", (event) => {
  if (event.target === els.productModal) closeModal();
});
els.productModal.addEventListener("cancel", (event) => {
  event.preventDefault();
  closeModal();
});

loadStats();
setupInfiniteScroll();
resetAndLoad();
