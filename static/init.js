(function () {
  const MOUNT_PREFIXES = ["/battlenetcodes"];

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
})();
