(function () {
  const MOUNT_PREFIXES = ["/battlenetcodes"];
  const PAGE_FADE_MS = 450;
  const BTN_STYLE_KEY = "bore-btn-style";

  function readBtnStyle() {
    try {
      return localStorage.getItem(BTN_STYLE_KEY) === "square" ? "square" : "pill";
    } catch {
      return "pill";
    }
  }

  function applyBtnStyle(style) {
    document.documentElement.dataset.btnStyle = style;
  }

  applyBtnStyle(readBtnStyle());

  function mountBtnStyleToggle(container) {
    if (!container || container.dataset.mounted) return;
    container.dataset.mounted = "1";
    const current = readBtnStyle();
    container.innerHTML =
      '<div class="btn-style-toggle" role="group" aria-label="Button shape">' +
      `<button type="button" class="btn-style-option${current === "pill" ? " is-active" : ""}" data-shape="pill" title="Rounded buttons">Round</button>` +
      `<button type="button" class="btn-style-option${current === "square" ? " is-active" : ""}" data-shape="square" title="Square buttons">Square</button>` +
      "</div>";
    container.addEventListener("click", (event) => {
      const btn = event.target.closest("[data-shape]");
      if (!btn) return;
      const shape = btn.dataset.shape;
      applyBtnStyle(shape);
      try {
        localStorage.setItem(BTN_STYLE_KEY, shape);
      } catch {
        /* ignore */
      }
      container.querySelectorAll(".btn-style-option").forEach((el) => {
        el.classList.toggle("is-active", el.dataset.shape === shape);
      });
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-btn-style-toggle]").forEach(mountBtnStyleToggle);
  });

  let baseHref = "/";
  for (const prefix of MOUNT_PREFIXES) {
    if (location.pathname === prefix || location.pathname.startsWith(`${prefix}/`)) {
      baseHref = `${prefix}/`;
      break;
    }
  }

  window.BASE_PATH = baseHref === "/" ? "" : baseHref.slice(0, -1);
  window.apiUrl = function apiUrl(path) {
    const normalized = path.startsWith("/") ? path : `/${path}`;
    return `${window.BASE_PATH}${normalized}`;
  };
  window.assetUrl = function assetUrl(path) {
    if (!path) return null;
    if (/^https?:\/\//i.test(path)) return path;
    const normalized = path.startsWith("/") ? path : `/${path}`;
    return `${window.BASE_PATH}${normalized}`;
  };

  const base = document.createElement("base");
  base.href = baseHref;
  document.head.appendChild(base);

  const embedQuery = new URLSearchParams(location.search);
  const isEmbed =
    embedQuery.get("embed") === "1" || window.self !== window.top;
  if (isEmbed) {
    document.documentElement.classList.add("scanner-embed-mode");
  }

  const prefersReducedMotion = window.matchMedia(
    "(prefers-reduced-motion: reduce)"
  ).matches;
  const transitionsEnabled = !isEmbed && !prefersReducedMotion;

  function leavePage(url) {
    const body = document.body;
    if (!body || !transitionsEnabled) {
      location.href = url;
      return;
    }
    body.classList.add("page-leaving");
    window.setTimeout(() => {
      location.href = url;
    }, PAGE_FADE_MS);
  }

  function shouldFadeNavigate(link, event) {
    if (!transitionsEnabled) return false;
    if (event.defaultPrevented) return false;
    if (event.button !== 0) return false;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return false;
    if (link.target && link.target !== "_self") return false;
    if (link.hasAttribute("download")) return false;

    const href = link.getAttribute("href");
    if (!href || href.startsWith("#") || href.startsWith("javascript:")) {
      return false;
    }

    const url = new URL(link.href, location.href);
    if (url.origin !== location.origin) return false;

    return true;
  }

  function onDocumentClick(event) {
    const link = event.target.closest("a[href]");
    if (!link || !shouldFadeNavigate(link, event)) return;
    event.preventDefault();
    leavePage(link.href);
  }

  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    document.body?.classList.remove("page-leaving");
  });

  document.addEventListener("click", onDocumentClick);
})();
