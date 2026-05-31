import asyncio
import html
import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin

import aiofiles
import httpx
from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from app.library import IMAGES_DIR, ensure_dirs
from app.games import detect_game

ROOT = Path(__file__).resolve().parent.parent
AUTH_PATH = ROOT / "data" / "auth.json"

REGIONS = {
    "us": "us.checkout.battle.net",
    "eu": "eu.checkout.battle.net",
    "kr": "kr.checkout.battle.net",
    "tw": "tw.checkout.battle.net",
}

BLOCKED_RESOURCE_TYPES: set[str] = set()
BLOCKED_URL_PARTS = (
    "googletagmanager.com",
    "google-analytics.com",
    "cookielaw.org",
    "onetrust",
    "liveperson.net",
    "lpTag",
    "rum.battle.net",
)


class ScanStatus(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    NEEDS_LOGIN = "needs_login"
    ERROR = "error"


@dataclass
class ScanResult:
    code: int
    valid: bool
    name: str | None = None
    price: str | None = None
    image_url: str | None = None
    image_path: str | None = None
    url: str | None = None
    status: str = "invalid"
    message: str | None = None
    game: str | None = None
    raw_notes: str | None = None
    checked_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "valid": self.valid,
            "name": self.name,
            "price": self.price,
            "image_url": self.image_url,
            "image_path": self.image_path,
            "url": self.url,
            "status": self.status,
            "message": self.message,
            "game": self.game,
            "raw_notes": self.raw_notes,
            "checked_at": self.checked_at,
        }


EXTRACT_SCRIPT = """
() => {
  const text = document.body?.innerText || '';
  const lower = text.toLowerCase();
  const title = document.title || '';

  if (
    /too many requests|rate limit|try again later|temporarily unavailable|service unavailable/i.test(lower)
  ) {
    return { kind: 'rate_limited', message: 'Rate limited by Battle.net — reduce parallel scans and retry later' };
  }

  if (
    lower.includes('log in or sign up') ||
    lower.includes('battle.net login') ||
    document.querySelector('input[name="accountName"], #accountName')
  ) {
    return { kind: 'login', message: 'Not logged in — open Login Browser and save your session' };
  }

  if (lower.includes('nothing here') || /^error \\|/i.test(title)) {
    let message = 'No product exists for this code (Battle.net: nothing here)';
    const errMatch = text.match(/error code:\\s*([A-Z0-9]+)/i);
    if (errMatch) message += ` · ${errMatch[1]}`;
    return { kind: 'empty', message };
  }

  const lines = text.split(/\\r?\\n/).map((line) => line.trim()).filter(Boolean);
  const skipLabels = new Set([
    'payment information',
    'checkout',
    'product summary',
    'you are purchasing',
    'total',
    'rating',
    'need help?',
    'we accept',
    'product requirements',
    'pay with',
    'details',
    'cancel',
    'pay now',
    'system requirements',
    'windows',
    'mac',
  ]);

  let name = null;
  const purchaseIdx = lines.findIndex((line) => line.toUpperCase() === 'YOU ARE PURCHASING');
  if (purchaseIdx >= 0) {
    for (let i = purchaseIdx + 1; i < lines.length; i += 1) {
      const line = lines[i];
      if (skipLabels.has(line.toLowerCase()) || line.length < 3) continue;
      if (/^[$€£¥₩]/.test(line)) continue;
      name = line;
      break;
    }
  }

  if (!name) {
    const titleMatch = title.match(/^Buy\\s+(.+?)\\s*\\|\\s*Battle\\.net Shop/i);
    if (titleMatch) name = titleMatch[1].trim();
  }

  if (name && (/^(500:|404|error|internal server)/i.test(name) || name.includes('nothing here'))) {
    name = null;
  }

  const onPayUrl = /\\/checkout\\/pay\\//i.test(window.location.pathname);
  const isCheckout =
    lower.includes('you are purchasing') ||
    lower.includes('payment information') ||
    onPayUrl;

  const parseAmount = (value) => {
    const num = parseFloat(String(value).replace(/[^0-9.]/g, ''));
    return Number.isFinite(num) ? num : 0;
  };

  const pickPrice = (amounts) => {
    const normalized = [...new Set(amounts.map((part) => part.replace(/\\s+/g, '')))];
    const priced = normalized.filter((part) => /^[$€£¥₩]/.test(part));
    if (!priced.length) return null;
    const positive = priced.filter((part) => parseAmount(part) > 0);
    positive.sort((a, b) => parseAmount(a) - parseAmount(b));
    if (positive.length >= 2) {
      return `${positive[0]} (was ${positive[positive.length - 1]})`;
    }
    if (positive.length === 1) return positive[0];
    const zeros = priced.filter((part) => parseAmount(part) === 0);
    if (zeros.length >= 1) return zeros[0];
    return null;
  };

  const collectMoney = (root) => {
    const amounts = [];
    const walk = (node) => {
      if (!node) return;
      if (node.shadowRoot) walk(node.shadowRoot);
      const children = node.children ? Array.from(node.children) : [];
      if (!children.length) {
        const value = (node.textContent || '').trim();
        if (/^[$€£¥₩][\\d][\\d.,]*/.test(value) && value.length < 24) {
          amounts.push(value.replace(/\\s+/g, ''));
        }
        return;
      }
      children.forEach(walk);
    };
    walk(root);
    return amounts;
  };

  let price = null;
  const priceLabels = Array.from(document.querySelectorAll('meka-price-label, MEKA-PRICE-LABEL'));
  for (const label of priceLabels) {
    const labelText = (label.shadowRoot?.textContent || label.textContent || '').replace(/\\s+/g, ' ');
    const parts = labelText.match(/[$€£¥₩]\\s*[\\d][\\d.,]*/g) || [];
    if (!parts.length) continue;
    const picked = pickPrice(parts);
    if (picked) {
      price = picked;
      break;
    }
  }

  if (!price) {
    const money = [...new Set(collectMoney(document.body))];
    price = pickPrice(money);
  }

  const pickCoinPrice = (coinName) => {
    const amounts = [];
    for (const label of priceLabels) {
      const text = (label.shadowRoot?.textContent || label.textContent || '').replace(/\\s+/g, ' ').trim();
      if (!text) continue;
      let raw = null;
      let val = 0;
      if (/^\\d[\\d,]*$/.test(text)) {
        raw = text;
        val = parseInt(text.replace(/,/g, ''), 10);
      } else {
        const embedded = text.match(/^(\\d[\\d,]+)/);
        if (embedded) {
          raw = embedded[1];
          val = parseInt(raw.replace(/,/g, ''), 10);
        }
      }
      if (val > 0) amounts.push({ raw, val });
    }
    if (amounts.length) {
      const maxVal = Math.max(...amounts.map((entry) => entry.val));
      const maxEntries = amounts.filter((entry) => entry.val === maxVal);
      const pick = maxEntries.length >= 2 ? maxEntries[0] : amounts.sort((a, b) => b.val - a.val)[0];
      return `${pick.raw} ${coinName}`;
    }
    const bodyMatch = text.match(/(\\d[\\d,]+)\\s*Overwatch[\\u00ae\\u2122]?\\s*Coins/i);
    if (bodyMatch) return `${bodyMatch[1]} Overwatch Coins`;
    const genericCoin = text.match(/(\\d[\\d,]+)\\s+([A-Za-z][A-Za-z0-9\\u00ae\\u2122\\s]{2,30}Coins)/i);
    if (genericCoin) return `${genericCoin[1]} ${genericCoin[2].replace(/\\s+/g, ' ').trim()}`;
    return null;
  };

  if (!price && /overwatch[\\u00ae\\u2122]?\\s*coins/i.test(lower)) {
    price = pickCoinPrice('Overwatch Coins');
  } else if (!price && /\\bcoins\\b/i.test(lower)) {
    price = pickCoinPrice('Coins');
  }

  const images = Array.from(document.querySelectorAll('img'))
    .map((img) => ({
      src: img.currentSrc || img.src,
      w: img.naturalWidth || img.width || 0,
      h: img.naturalHeight || img.height || 0,
    }))
    .filter(
      (img) =>
        img.src &&
        !/404|logo|icon|spinner|favicon|payment|visa|mastercard|paypal|apple-pay|google-pay|discover|amex|jcb|diners|unionpay|peg|opengraph|\/sprites?\//i.test(
          img.src,
        ) &&
        !/catalog\\.blzstatic\\.com\\/?\\?/i.test(img.src),
    )
    .sort((a, b) => {
      const score = (img) => {
        let value = img.w * img.h;
        if (/catalog\\.blzstatic\\.com|blzstatic\\.com/.test(img.src)) value += 1e9;
        return value;
      };
      return score(b) - score(a);
    });

  let imageUrl = images.length ? images[0].src : null;
  if (!imageUrl) {
    const catalogImg = images.find((img) => /catalog\\.blzstatic\\.com/i.test(img.src) && /prod-thumb|prod_thumb|shopproductpage/i.test(img.src));
    if (catalogImg) imageUrl = catalogImg.src;
  }

  let kind = 'invalid';
  const onPreloadUrl = /\\/checkout\\/preload\\//i.test(window.location.pathname);
  if (isCheckout && name) {
    kind = 'valid';
  } else if (name && (onPayUrl || onPreloadUrl || lower.includes('payment information'))) {
    kind = 'valid';
  } else if (onPayUrl && (price || lower.includes('product summary'))) {
    kind = 'partial';
  } else if (onPreloadUrl || lower.includes('payment information')) {
    kind = 'partial';
  } else if (name && price && /coins/i.test(price)) {
    kind = 'valid';
  }

  let genre = null;
  const genreIdx = lines.findIndex((line) => line.toLowerCase() === 'genre');
  if (genreIdx >= 0) {
    for (let i = genreIdx + 1; i < lines.length; i += 1) {
      const line = lines[i];
      if (!line) continue;
      const normalized = line.toLowerCase();
      if (normalized === 'platforms' || normalized === 'platform' || normalized === 'product details') break;
      genre = line;
      break;
    }
  }

  return {
    kind,
    name,
    price,
    imageUrl,
    genre,
    pageText: text.slice(0, 6000),
    requirementsText: (() => {
      const start = lines.findIndex((line) => /product requirements/i.test(line));
      if (start < 0) return '';
      return lines.slice(start, start + 10).join('\\n');
    })(),
    message:
      kind === 'partial'
        ? 'Product exists but checkout details were not fully detected'
        : kind === 'valid'
          ? null
          : name
            ? 'Page loaded but this is not a checkout product page'
            : 'Page loaded but no product was detected',
  };
}
"""


MAX_SCAN_RETRIES = 2
SCAN_TIMEOUT_SEC = 45
HTTP_PROBE_TIMEOUT_MS = 20000
HTTP_PROBE_RETRIES = 2
HTTP_PROBE_MAX_REDIRECTS = 30
HTTP_PROBE_CONCURRENCY = 16
BROWSER_POOL_MAX = 8
FAST_POLL_INTERVAL_SEC = 0.025
FAST_POLL_MAX_SEC = 2.5
TITLE_BUY_RE = re.compile(r"Buy\s+(.+?)\s*\|\s*Battle\.net Shop", re.I | re.S)
OG_TITLE_RE = re.compile(
    r'property=["\']og:title["\']\s+content=["\']([^"\']+)["\']|content=["\']([^"\']+)["\']\s+property=["\']og:title["\']',
    re.I,
)
OG_IMAGE_RE = re.compile(
    r'property=["\']og:image["\']\s+content=["\']([^"\']+)["\']|content=["\']([^"\']+)["\']\s+property=["\']og:image["\']',
    re.I,
)
CATALOG_IMG_RE = re.compile(
    r'https?://[^"\']*catalog\.blzstatic\.com[^"\']*(?:prod-thumb|prod_thumb|shopproductpage)[^"\']*',
    re.I,
)
HTML_IMG_RE = re.compile(
    r'https?://[^"\']*blzstatic\.com[^"\']+\.(?:jpg|jpeg|png|webp)(?:\?[^"\']*)?',
    re.I,
)
JSON_PRICE_RE = re.compile(r'"price"\s*:\s*"?([\d.]+)"?', re.I)
HTML_MONEY_RE = re.compile(r'[$€£¥₩]\s*[\d][\d.,]*')
COIN_BODY_RE = re.compile(
    r'(\d[\d,]+)\s+(Overwatch[\u00ae\u2122\s]*Coins)',
    re.I,
)
CP_BODY_RE = re.compile(
    r'(\d[\d,]+)\s*(?:\(\+\s*(\d[\d,]+)\s*Bonus\s*\))?\s*CP\b',
    re.I,
)
GENRE_HTML_RE = re.compile(r'"genre"\s*:\s*"([^"]+)"', re.I)
JSON_NAME_RE = re.compile(r'"name"\s*:\s*"([^"\\]{3,120})"', re.I)
BLZ_ERROR_RE = re.compile(r"error code:\s*([A-Z0-9]+)", re.I)
RATE_LIMIT_TEXT_RE = re.compile(
    r"too many requests|rate limit|try again later|temporarily unavailable|service unavailable",
    re.I,
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _extract_product_name_from_html(text: str) -> str | None:
    match = TITLE_BUY_RE.search(text)
    if match:
        name = html.unescape(match.group(1).strip())
        if name and "nothing here" not in name.lower():
            return name
    og = OG_TITLE_RE.search(text)
    if og:
        name = html.unescape((og.group(1) or og.group(2) or "").strip())
        lower = name.lower()
        if name and "battle.net shop" not in lower and "nothing here" not in lower:
            return name
    for match in JSON_NAME_RE.finditer(text):
        name = html.unescape(match.group(1).strip())
        lower = name.lower()
        if len(name) > 3 and "battle.net" not in lower and "nothing here" not in lower:
            return name
    return None


def _blz_error_code(text: str) -> str | None:
    match = BLZ_ERROR_RE.search(text)
    return match.group(1) if match else None


def _empty_product_message(text: str) -> str:
    message = "No product exists for this code (Battle.net: nothing here)"
    code = _blz_error_code(text)
    if code:
        message += f" · {code}"
    return message


def _extract_image_url_from_html(text: str) -> str | None:
    match = OG_IMAGE_RE.search(text)
    if match:
        url = html.unescape((match.group(1) or match.group(2) or "").strip())
        if url and "opengraph" not in url.lower():
            return url
    catalog = CATALOG_IMG_RE.search(text)
    if catalog:
        return html.unescape(catalog.group(0).strip())
    for img_match in HTML_IMG_RE.finditer(text):
        url = html.unescape(img_match.group(0).strip())
        if any(
            skip in url.lower()
            for skip in ("logo", "icon", "favicon", "payment", "visa", "mastercard", "paypal", "peg")
        ):
            continue
        return url
    return None


def _parse_amount(value: str) -> float:
    try:
        return float(re.sub(r"[^0-9.]", "", value))
    except ValueError:
        return 0.0


def _extract_price_from_html(text: str, name: str | None) -> str | None:
    haystack = f"{name or ''} {text[:12000]}"
    lower = haystack.lower()

    coin_match = COIN_BODY_RE.search(haystack)
    if coin_match or "overwatch" in lower and "coins" in lower:
        if coin_match:
            return f"{coin_match.group(1)} {coin_match.group(2).strip()}"
        generic = re.search(r"(\d[\d,]+)\s+([A-Za-z][A-Za-z0-9\u00ae\u2122\s]{2,30}Coins)", haystack, re.I)
        if generic:
            return f"{generic.group(1)} {generic.group(2).strip()}"

    cp_match = CP_BODY_RE.search(haystack)
    if cp_match:
        base, bonus = cp_match.group(1), cp_match.group(2)
        if bonus:
            return f"{base} (+{bonus} Bonus) CP"
        return f"{cp_match.group(1)} CP"

    if name:
        name_cp = re.search(r"(\d[\d,]+)\s*(?:\(\+\s*(\d[\d,]+)\s*Bonus\s*\))?\s*CP", name, re.I)
        if name_cp:
            if name_cp.group(2):
                return f"{name_cp.group(1)} (+{name_cp.group(2)} Bonus) CP"
            return f"{name_cp.group(1)} CP"

    for json_match in JSON_PRICE_RE.finditer(text):
        amount = _parse_amount(json_match.group(1))
        if amount > 0:
            return f"${amount:.2f}" if amount < 1000 else f"${amount:,.2f}".replace(".00", "")

    money = []
    for money_match in HTML_MONEY_RE.finditer(text[:20000]):
        part = money_match.group(0).replace(" ", "")
        amount = _parse_amount(part)
        if amount >= 0:
            money.append((amount, part))
    if money:
        positive = [entry for entry in money if entry[0] > 0]
        if positive:
            positive.sort(key=lambda entry: entry[0])
            if len(positive) >= 2:
                return f"{positive[0][1]} (was {positive[-1][1]})"
            return positive[0][1]
        zero = [entry for entry in money if entry[0] == 0]
        if zero:
            return zero[0][1]
    return None


def _extract_genre_from_html(text: str) -> str | None:
    match = GENRE_HTML_RE.search(text)
    if match:
        return html.unescape(match.group(1).strip())
    return None


def _looks_rate_limited(text: str, status_code: int, headers: dict[str, str] | None = None) -> bool:
    if status_code in (429, 502, 503, 504):
        return True
    if headers and headers.get("retry-after"):
        return True
    return bool(RATE_LIMIT_TEXT_RE.search(text))


def _rate_limit_message(status_code: int | None = None) -> str:
    if status_code == 429:
        return "Rate limited by Battle.net (HTTP 429) — reduce parallel scans and retry later"
    if status_code in (502, 503, 504):
        return f"Battle.net temporarily unavailable (HTTP {status_code}) — likely rate limited, retry later"
    return "Rate limited or throttled by Battle.net — reduce parallel scans and retry later"


def _server_error_message(status_code: int) -> str:
    return f"Battle.net server error (HTTP {status_code}) — retry later"


def _is_transient_probe_error(exc: Exception) -> bool:
    detail = str(exc).lower()
    return any(
        part in detail
        for part in (
            "max redirect",
            "too many redirects",
            "connection closed",
            "reading from the driver",
            "timeout",
            "timed out",
            "connect",
            "connection reset",
            "broken pipe",
            "remote protocol",
            "connection aborted",
        )
    )


def _transient_probe_message(exc: Exception) -> str:
    detail = str(exc).split("\n", 1)[0]
    if "max redirect" in detail.lower() or "too many redirect" in detail.lower():
        return "Too many redirects — Battle.net may be rate limiting, retry with fewer parallel scans"
    if "connection closed" in detail.lower() or "driver" in detail.lower():
        return "Scanner overloaded — reduce parallel scans and recheck these codes"
    if "timeout" in detail.lower():
        return "Request timed out — likely rate limited or network issue, retry later"
    return "Temporary network issue — recheck this code later"


def _should_retry_scan(result: ScanResult) -> bool:
    if result.status in ("needs_login", "empty"):
        return False
    if result.status in ("rate_limited", "error", "server_error", "timeout"):
        return True
    if result.url and "chrome-error://" in result.url:
        return True
    if result.url and "/checkout/pay/" in result.url and result.status == "invalid":
        return True
    if result.url and "/checkout/pay/" in result.url and not result.price:
        return True
    if result.status == "invalid" and result.valid is False and result.name:
        return True
    if result.url and "/checkout/pay/" in result.url and result.status == "partial" and not result.price:
        return True
    return False


class BattleNetScanner:
    def __init__(self) -> None:
        ensure_dirs()
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._httpx: httpx.AsyncClient | None = None
        self._browser_lock = asyncio.Lock()
        self._nav_semaphore = asyncio.Semaphore(BROWSER_POOL_MAX)
        self._enrich_semaphore = asyncio.Semaphore(1)
        self._http_semaphore = asyncio.Semaphore(HTTP_PROBE_CONCURRENCY)
        self._code_locks: dict[int, asyncio.Lock] = {}
        self._active_headless: bool | None = None
        self._page_pool: asyncio.Queue[Page] = asyncio.Queue()
        self._pool_size = 0
        self._login_page: Page | None = None
        self.status = ScanStatus.IDLE
        self._auto_task: asyncio.Task | None = None
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._stop_requested = False
        self._on_event: Callable[[str, dict[str, Any]], Any] | None = None
        self.session_started_at: str | None = None
        self._active_scans: dict[int, str] = {}
        self._activity_lock = asyncio.Lock()
        self.session_codes_done = 0
        self.session_codes_total = 0
        self._scan_durations: deque[float] = deque(maxlen=100)
        self._fast_scan = False
        self._progress_emit_interval = 0.2
        self._last_progress_emit = 0.0

    def touch_session(self, *, reset: bool = False) -> str:
        if reset or not self.session_started_at:
            self.session_started_at = datetime.now(timezone.utc).isoformat()
            self.session_codes_done = 0
            self.session_codes_total = 0
            self._scan_durations.clear()
        return self.session_started_at

    def activity_snapshot(self) -> dict[str, Any]:
        eta_seconds = None
        avg_seconds = None
        if self._scan_durations:
            avg_seconds = sum(self._scan_durations) / len(self._scan_durations)
        elif self.session_started_at and self.session_codes_done > 0:
            start = datetime.fromisoformat(self.session_started_at)
            elapsed = (datetime.now(timezone.utc) - start).total_seconds()
            avg_seconds = elapsed / self.session_codes_done
        if avg_seconds and self.session_codes_done > 0:
            remaining = max(0, self.session_codes_total - self.session_codes_done)
            if remaining > 0:
                eta_seconds = int(remaining * avg_seconds)
        scans_per_second = None
        if self.session_started_at and self.session_codes_done > 0:
            start = datetime.fromisoformat(self.session_started_at)
            elapsed = (datetime.now(timezone.utc) - start).total_seconds()
            if elapsed >= 0.5:
                scans_per_second = round(self.session_codes_done / elapsed, 1)
        return {
            "session_started_at": self.session_started_at,
            "active_scans": {str(code): started for code, started in self._active_scans.items()},
            "session_codes_done": self.session_codes_done,
            "session_codes_total": self.session_codes_total,
            "eta_seconds": eta_seconds,
            "avg_seconds_per_scan": round(avg_seconds, 2) if avg_seconds else None,
            "scans_per_second": scans_per_second,
        }

    @property
    def is_logged_in(self) -> bool:
        return AUTH_PATH.exists()

    def set_event_handler(self, handler: Callable[[str, dict[str, Any]], Any] | None) -> None:
        self._on_event = handler

    async def _emit(self, event: str, payload: dict[str, Any], *, wait: bool = False) -> None:
        if not self._on_event:
            return

        async def run() -> None:
            try:
                result = self._on_event(event, payload)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass

        if wait:
            await run()
        else:
            asyncio.create_task(run())

    async def _route_handler(self, route) -> None:
        request = route.request
        blocked_types = ("font", "media", "websocket", "manifest")
        if self._fast_scan:
            blocked_types = ("image", "stylesheet", "font", "media", "websocket", "manifest")
        if request.resource_type in blocked_types:
            await route.abort()
            return
        if any(part in request.url for part in BLOCKED_URL_PARTS):
            await route.abort()
            return
        await route.continue_()

    async def _ensure_playwright(self) -> None:
        if not self._playwright:
            self._playwright = await async_playwright().start()

    def _load_auth_cookies(self) -> httpx.Cookies:
        jar = httpx.Cookies()
        if not AUTH_PATH.exists():
            return jar
        try:
            data = json.loads(AUTH_PATH.read_text(encoding="utf-8"))
        except Exception:
            return jar
        for cookie in data.get("cookies", []):
            name = cookie.get("name")
            value = cookie.get("value")
            if not name or value is None:
                continue
            domain = (cookie.get("domain") or "").lstrip(".")
            path = cookie.get("path") or "/"
            try:
                jar.set(name, value, domain=domain or None, path=path)
            except Exception:
                continue
        return jar

    def _httpx_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            cookies=self._load_auth_cookies(),
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            follow_redirects=True,
            max_redirects=HTTP_PROBE_MAX_REDIRECTS,
            timeout=httpx.Timeout(HTTP_PROBE_TIMEOUT_MS / 1000, connect=10.0),
        )

    async def _dispose_httpx(self) -> None:
        if self._httpx:
            await self._httpx.aclose()
            self._httpx = None

    async def _http_get(self, url: str) -> tuple[int, str, str, dict[str, str]] | None:
        last_error: Exception | None = None
        async with self._http_semaphore:
            for attempt in range(HTTP_PROBE_RETRIES):
                try:
                    async with self._httpx_client() as client:
                        response = await client.get(url)
                        return (
                            response.status_code,
                            str(response.url),
                            response.text,
                            {k.lower(): v for k, v in response.headers.items()},
                        )
                except httpx.TooManyRedirects as exc:
                    last_error = exc
                except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
                    last_error = exc
                except Exception as exc:
                    if _is_transient_probe_error(exc):
                        last_error = exc
                    else:
                        raise
                if attempt + 1 < HTTP_PROBE_RETRIES:
                    await asyncio.sleep(0.35 * (attempt + 1))
        if last_error is not None:
            return None
        return None

    async def _configure_page(self, page: Page) -> None:
        await page.route("**/*", self._route_handler)

    async def _close_browser(self) -> None:
        await self._dispose_httpx()
        while not self._page_pool.empty():
            page = await self._page_pool.get()
            if not page.is_closed():
                await page.close()
        self._pool_size = 0
        if self._login_page and not self._login_page.is_closed():
            await self._login_page.close()
        self._login_page = None
        if self._context:
            await self._context.close()
            self._context = None
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        self._active_headless = None

    async def _ensure_browser(self, headless: bool = True) -> BrowserContext:
        async with self._browser_lock:
            if self._context and self._active_headless == headless:
                return self._context

            if self._context:
                while not self._page_pool.empty():
                    page = await self._page_pool.get()
                    if not page.is_closed():
                        await page.close()
                self._pool_size = 0
                if self._login_page and not self._login_page.is_closed():
                    await self._login_page.close()
                self._login_page = None
                await self._context.close()
                self._context = None
                if self._browser:
                    await self._browser.close()
                    self._browser = None

            await self._ensure_playwright()
            self._browser = await self._playwright.chromium.launch(
                headless=headless,
                args=[
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-background-networking",
                ],
            )
            context_kwargs: dict[str, Any] = {
                "viewport": {"width": 1280, "height": 800},
                "user_agent": USER_AGENT,
            }
            if AUTH_PATH.exists():
                context_kwargs["storage_state"] = str(AUTH_PATH)

            self._context = await self._browser.new_context(**context_kwargs)
            self._active_headless = headless
            return self._context

    async def _borrow_page(self, headless: bool = True) -> Page:
        await self._ensure_browser(headless=headless)
        try:
            return self._page_pool.get_nowait()
        except asyncio.QueueEmpty:
            page = await self._context.new_page()
            await self._configure_page(page)
            self._pool_size += 1
            return page

    async def _return_page(self, page: Page) -> None:
        if page.is_closed():
            return
        await self._page_pool.put(page)

    async def _ensure_pool_size(self, size: int, headless: bool) -> None:
        await self._ensure_browser(headless=headless)
        while self._pool_size < size:
            page = await self._context.new_page()
            await self._configure_page(page)
            await self._page_pool.put(page)
            self._pool_size += 1

    async def close(self) -> None:
        await self._stop_running_auto()
        async with self._browser_lock:
            await self._close_browser()
        self.status = ScanStatus.IDLE

    async def open_login_browser(self) -> None:
        async with self._browser_lock:
            await self._close_browser()
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(headless=False)
            self._context = await self._browser.new_context(viewport={"width": 1400, "height": 900})
            self._login_page = await self._context.new_page()
            await self._login_page.goto(
                "https://us.checkout.battle.net/shop/en/checkout/buy/64313",
                wait_until="domcontentloaded",
            )
            self._active_headless = False
            self.status = ScanStatus.NEEDS_LOGIN
            await self._emit("login_browser_opened", {"message": "Log in, then click Save Login Session."})

    async def save_login_session(self) -> bool:
        async with self._browser_lock:
            if not self._context:
                return False
            await self._context.storage_state(path=str(AUTH_PATH))
            await self._dispose_httpx()
            await self._emit("login_saved", {"path": str(AUTH_PATH)})
            self.status = ScanStatus.IDLE
            return True

    def checkout_url(self, code: int, region: str = "us") -> str:
        host = REGIONS.get(region, REGIONS["us"])
        return f"https://{host}/shop/en/checkout/buy/{code}"

    def _buy_url_unchanged(self, code: int, final_url: str) -> bool:
        normalized = final_url.split("?", 1)[0].rstrip("/")
        return normalized.endswith(f"/buy/{code}")

    def _parse_http_preload_valid(
        self,
        code: int,
        status_code: int,
        final_url: str,
        text: str,
    ) -> ScanResult | None:
        if status_code != 200:
            return None
        if "/checkout/preload/" not in final_url and "/checkout/pay/" not in final_url:
            return None
        name = _extract_product_name_from_html(text)
        return ScanResult(
            code=code,
            valid=True,
            name=name,
            url=final_url,
            status="valid" if name else "partial",
            message=None,
        )

    def _parse_http_valid(self, code: int, final_url: str, text: str) -> ScanResult | None:
        title_match = TITLE_BUY_RE.search(text)
        if not title_match:
            return None
        name = html.unescape(title_match.group(1).strip())
        if not name or "nothing here" in name.lower():
            return None
        on_product_page = (
            "/checkout/preload/" in final_url
            or "/checkout/pay/" in final_url
            or "you are purchasing" in text.lower()
        )
        if not on_product_page and not self._buy_url_unchanged(code, final_url):
            on_product_page = True
        if not on_product_page:
            return None
        return ScanResult(
            code=code,
            valid=True,
            name=name,
            url=final_url,
            status="valid",
            message=None,
        )

    def _is_definite_http_invalid(self, code: int, status_code: int, final_url: str, text: str) -> bool:
        if "/checkout/preload/" in final_url or "/checkout/pay/" in final_url or "/checkout/dm" in final_url:
            return False
        if not self._buy_url_unchanged(code, final_url):
            return False
        lower = text.lower()
        if "nothing here" in lower or "error |" in lower[:800]:
            return True
        return status_code == 404

    def _classify_http_failure(
        self,
        code: int,
        status_code: int,
        final_url: str,
        text: str,
        headers: dict[str, str],
    ) -> ScanResult | None:
        lower = text.lower()
        final_lower = final_url.lower()

        if (
            "account.battle.net/login" in final_lower
            or "battle.net login" in lower
            or "log in or sign up" in lower
        ):
            self.status = ScanStatus.NEEDS_LOGIN
            return ScanResult(
                code=code,
                valid=False,
                url=final_url,
                status="needs_login",
                message="Not logged in — open Login Browser and save your session",
            )

        if _looks_rate_limited(text, status_code, headers):
            return ScanResult(
                code=code,
                valid=False,
                url=final_url,
                status="rate_limited",
                message=_rate_limit_message(status_code),
            )

        if self._is_definite_http_invalid(code, status_code, final_url, text):
            return ScanResult(
                code=code,
                valid=False,
                url=final_url,
                status="empty",
                message=_empty_product_message(text),
            )

        if status_code == 403:
            return ScanResult(
                code=code,
                valid=False,
                url=final_url,
                status="rate_limited",
                message="Access blocked (HTTP 403) — possible rate limit or session issue",
            )

        if status_code >= 500:
            return ScanResult(
                code=code,
                valid=False,
                url=final_url,
                status="server_error",
                message=_server_error_message(status_code),
            )

        return None

    async def _http_probe(self, code: int, region: str) -> ScanResult | None:
        url = self.checkout_url(code, region)
        fetched = await self._http_get(url)
        if fetched is None:
            return None

        status_code, final_url, text, headers = fetched

        valid_result = self._parse_http_valid(code, final_url, text)
        if valid_result:
            return await self._enrich_http_result(valid_result, text, final_url)

        failure = self._classify_http_failure(code, status_code, final_url, text, headers)
        if failure:
            return failure

        preload = self._parse_http_preload_valid(code, status_code, final_url, text)
        if preload:
            return await self._enrich_http_result(preload, text, final_url)

        if status_code == 200 or "/checkout/preload/" in final_url or "/checkout/pay/" in final_url:
            return None

        return None

    async def _wait_for_price_labels(self, page: Page, *, timeout_ms: int = 8000) -> None:
        try:
            await page.wait_for_function(
                """() => {
                  const parseAmount = (value) => {
                    const num = parseFloat(String(value).replace(/[^0-9.]/g, ''));
                    return Number.isFinite(num) ? num : 0;
                  };
                  const lower = (document.body?.innerText || '').toLowerCase();
                  if (lower.includes('nothing here') || lower.includes('log in or sign up')) return true;
                  const labels = Array.from(document.querySelectorAll('meka-price-label, MEKA-PRICE-LABEL'));
                  if (!labels.length) return false;
                  const usesCoins = /overwatch[\u00ae\u2122]?\s*coins/i.test(lower) || /\bcoins\b/i.test(lower);
                  if (usesCoins) {
                    if (labels.length < 6) return false;
                    const coinVals = [];
                    for (const el of labels) {
                      const text = (el.shadowRoot?.textContent || el.textContent || '').trim();
                      if (/^\d[\d,]+$/.test(text)) {
                        const val = parseInt(text.replace(/,/g, ''), 10);
                        if (val > 0) coinVals.push(val);
                      }
                    }
                    if (!coinVals.length) return false;
                    const max = Math.max(...coinVals);
                    const countMax = coinVals.filter((v) => v === max).length;
                    return countMax >= 2 || max >= 2500;
                  }
                  for (const el of labels) {
                    const text = (el.shadowRoot?.textContent || el.textContent || '').trim();
                    const match = text.match(/[$€£¥₩]\s*[\d][\d.,]*/);
                    if (match && parseAmount(match[0]) > 0) return true;
                    if (usesCoins && /^\d[\d,]+$/.test(text) && parseInt(text.replace(/,/g, ''), 10) > 0) return true;
                  }
                  return false;
                }""",
                timeout=timeout_ms,
                polling=100,
            )
        except Exception:
            pass

    async def _poll_checkout_ready(self, page: Page) -> None:
        if page.url.startswith("chrome-error://"):
            return

        deadline = time.monotonic() + (FAST_POLL_MAX_SEC if self._fast_scan else 8.0)
        interval = FAST_POLL_INTERVAL_SEC if self._fast_scan else 0.1

        while time.monotonic() < deadline:
            try:
                ready = await page.evaluate(
                    """() => {
                      const text = document.body?.innerText || '';
                      const lower = text.toLowerCase();
                      if (!text.length) return false;
                      if (lower.includes('nothing here')) return true;
                      if (lower.includes('log in or sign up')) return true;
                      if (lower.includes('you are purchasing')) return true;
                      if (/^buy\\s/i.test(document.title || '')) return true;
                      if (/\\/checkout\\/pay\\//i.test(window.location.pathname)) return true;
                      return false;
                    }"""
                )
                if ready:
                    if not self._fast_scan:
                        await self._wait_for_price_labels(page)
                    return
            except Exception:
                pass
            await asyncio.sleep(interval)

    async def _browser_enrich_valid(
        self,
        code: int,
        region: str,
        headless: bool,
        base: ScanResult,
    ) -> ScanResult:
        async with self._enrich_semaphore:
            async with self._nav_semaphore:
                page = await self._borrow_page(headless=headless)
                try:
                    target = base.url or self.checkout_url(code, region)
                    if "/checkout/preload/" in target:
                        target = self.checkout_url(code, region)
                    try:
                        await page.goto(target, wait_until="domcontentloaded", timeout=20000)
                    except Exception as exc:
                        detail = str(exc).split("\n", 1)[0][:200].lower()
                        if "too_many_redirects" in detail or "err_too_many_redirects" in detail:
                            return base
                        if "net::" in detail or "timeout" in detail:
                            return base
                        raise

                    old_fast = self._fast_scan
                    self._fast_scan = False
                    try:
                        await self._poll_checkout_ready(page)
                        await self._wait_for_price_labels(page, timeout_ms=8000)
                    finally:
                        self._fast_scan = old_fast

                    extracted = await page.evaluate(EXTRACT_SCRIPT)
                    kind = extracted.get("kind")
                    if kind in ("login", "rate_limited", "empty"):
                        return base

                    if kind not in ("valid", "partial"):
                        return base

                    name = extracted.get("name") or base.name
                    price = extracted.get("price")
                    image_url = extracted.get("imageUrl")
                    image_path = None
                    if image_url:
                        image_path = await self._download_image(page, image_url, code)

                    page_text = extracted.get("pageText") or ""
                    requirements_text = extracted.get("requirementsText") or ""
                    genre = extracted.get("genre")
                    notes_parts: list[str] = []
                    if requirements_text:
                        notes_parts.append(requirements_text)
                    if genre:
                        notes_parts.append(f"Genre: {genre}")
                    raw_notes = "\n".join(notes_parts) or None
                    game = detect_game(name, f"{page_text}\n{requirements_text}", genre=genre)

                    return ScanResult(
                        code=code,
                        valid=True,
                        name=name,
                        price=price,
                        image_url=image_url,
                        image_path=image_path,
                        url=page.url or base.url,
                        status="valid" if kind == "valid" else "partial",
                        message=None,
                        game=game,
                        raw_notes=raw_notes,
                        checked_at=base.checked_at,
                    )
                except Exception:
                    return base
                finally:
                    await self._return_page(page)

    async def _browser_scan(self, code: int, region: str, headless: bool) -> ScanResult:
        async with self._nav_semaphore:
            page = await self._borrow_page(headless=headless)
            try:
                return await self._scan_with_page(page, code, region)
            finally:
                await self._return_page(page)

    async def _download_image_http(self, image_url: str, code: int, referer: str | None = None) -> str | None:
        if not image_url:
            return None
        absolute = urljoin(referer or "https://us.checkout.battle.net/", image_url)
        try:
            async with self._httpx_client() as client:
                response = await client.get(
                    absolute,
                    headers={"Referer": referer or "", "Accept": "image/*,*/*"},
                )
                if response.status_code != 200:
                    return None
                body = response.content
                if len(body) < 256:
                    return None
                content_type = response.headers.get("content-type", "")
                ext = ".jpg"
                if "png" in content_type:
                    ext = ".png"
                elif "webp" in content_type:
                    ext = ".webp"
                path = IMAGES_DIR / f"{code}{ext}"
                async with aiofiles.open(path, "wb") as f:
                    await f.write(body)
                return str(path.relative_to(ROOT)).replace("\\", "/")
        except Exception:
            return None

    async def _enrich_http_result(self, result: ScanResult, text: str, final_url: str) -> ScanResult:
        if not result.valid:
            return result

        name = result.name
        if not name:
            name = _extract_product_name_from_html(text)
            result.name = name

        if not result.price:
            result.price = _extract_price_from_html(text, name)

        if not result.image_url:
            result.image_url = _extract_image_url_from_html(text)

        if result.image_url and not result.image_path:
            result.image_path = await self._download_image_http(result.image_url, result.code, referer=final_url)

        genre = _extract_genre_from_html(text)
        requirements_bits: list[str] = []
        if genre:
            requirements_bits.append(f"Genre: {genre}")
        if requirements_bits:
            result.raw_notes = "\n".join(requirements_bits)

        if not result.game and name:
            result.game = detect_game(name, text[:6000], genre=genre)

        if result.valid and result.name and result.status == "partial":
            result.status = "valid"
            result.message = None

        return result

    async def _download_image(self, page: Page, image_url: str, code: int) -> str | None:
        if not image_url:
            return None
        absolute = urljoin(page.url, image_url)
        try:
            response = await page.request.get(absolute)
            if not response.ok:
                return None
            body = await response.body()
            content_type = response.headers.get("content-type", "")
            ext = ".jpg"
            if "png" in content_type:
                ext = ".png"
            elif "webp" in content_type:
                ext = ".webp"
            path = IMAGES_DIR / f"{code}{ext}"
            async with aiofiles.open(path, "wb") as f:
                await f.write(body)
            return str(path.relative_to(ROOT)).replace("\\", "/")
        except Exception:
            return None

    async def _scan_with_page(self, page: Page, code: int, region: str) -> ScanResult:
        url = self.checkout_url(code, region)
        try:
            wait_until = "commit" if self._fast_scan else "domcontentloaded"
            goto_timeout = 12000 if self._fast_scan else 20000
            await page.goto(url, wait_until=wait_until, timeout=goto_timeout)
            await self._poll_checkout_ready(page)
            if not self._fast_scan:
                await self._wait_for_price_labels(page, timeout_ms=12000)

            final_url = page.url
            if final_url.startswith("chrome-error://"):
                return ScanResult(
                    code=code,
                    valid=False,
                    url=final_url,
                    status="rate_limited",
                    message="Temporary network error — will recheck automatically",
                )

            extracted = await page.evaluate(EXTRACT_SCRIPT)
            final_url = page.url
            page_text = extracted.get("pageText") or ""
            if extracted.get("kind") == "login":
                self.status = ScanStatus.NEEDS_LOGIN
                return ScanResult(
                    code=code,
                    valid=False,
                    url=final_url,
                    status="needs_login",
                    message=extracted.get("message") or "Not logged in — open Login Browser and save your session",
                )

            if extracted.get("kind") == "rate_limited":
                return ScanResult(
                    code=code,
                    valid=False,
                    url=final_url,
                    status="rate_limited",
                    message=extracted.get("message") or _rate_limit_message(),
                )

            if extracted.get("kind") == "empty":
                return ScanResult(
                    code=code,
                    valid=False,
                    url=final_url,
                    status="empty",
                    message=extracted.get("message") or _empty_product_message(page_text),
                )

            kind = extracted.get("kind")
            name = extracted.get("name")
            if kind == "invalid" and name and (
                "/checkout/preload/" in final_url or "/checkout/pay/" in final_url
            ):
                kind = "valid"
            valid = kind in ("valid", "partial")
            status = kind if kind in ("valid", "partial", "invalid", "empty", "rate_limited") else "invalid"
            price = extracted.get("price")
            image_url = extracted.get("imageUrl")
            image_path = None
            if valid and image_url and not self._fast_scan:
                image_path = await self._download_image(page, image_url, code)

            page_text = extracted.get("pageText") or ""
            requirements_text = extracted.get("requirementsText") or ""
            genre = extracted.get("genre")
            notes_parts: list[str] = []
            if requirements_text:
                notes_parts.append(requirements_text)
            if genre:
                notes_parts.append(f"Genre: {genre}")
            raw_notes = "\n".join(notes_parts) or None
            game = detect_game(name, f"{page_text}\n{requirements_text}", genre=genre) if valid else None

            return ScanResult(
                code=code,
                valid=valid,
                name=name,
                price=price,
                image_url=image_url,
                image_path=image_path,
                url=final_url,
                status=status,
                message=extracted.get("message"),
                game=game,
                raw_notes=raw_notes,
            )
        except Exception as exc:
            detail = str(exc).split("\n", 1)[0][:200]
            lower = detail.lower()
            if "too_many_redirects" in lower or "err_too_many_redirects" in lower:
                return ScanResult(
                    code=code,
                    valid=False,
                    url=url,
                    status="rate_limited",
                    message="Temporary redirect error — will recheck automatically",
                )
            if "timeout" in lower or "timed out" in lower:
                return ScanResult(
                    code=code,
                    valid=False,
                    url=url,
                    status="timeout",
                    message="Page load timed out — likely rate limited, recheck with fewer scans",
                )
            if "net::" in lower or "connection" in lower:
                return ScanResult(
                    code=code,
                    valid=False,
                    url=url,
                    status="rate_limited",
                    message="Network blocked or overloaded — reduce parallel scans and recheck",
                )
            return ScanResult(
                code=code,
                valid=False,
                url=url,
                status="error",
                message=f"Scan error: {detail[:120]}",
            )

    def _code_lock(self, code: int) -> asyncio.Lock:
        if code not in self._code_locks:
            self._code_locks[code] = asyncio.Lock()
        return self._code_locks[code]

    async def _stop_running_auto(self) -> None:
        self._stop_requested = True
        self._pause_event.set()
        if self._auto_task and not self._auto_task.done():
            self._auto_task.cancel()
            try:
                await asyncio.wait_for(self._auto_task, timeout=8)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        self._auto_task = None
        self._stop_requested = False
        async with self._activity_lock:
            self._active_scans.clear()

    async def scan_code(self, code: int, region: str = "us", headless: bool = True) -> ScanResult:
        try:
            return await asyncio.wait_for(
                self._scan_code_impl(code, region, headless),
                timeout=SCAN_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            async with self._activity_lock:
                self._active_scans.pop(code, None)
            await self._emit(
                "scan_finished",
                {"code": code, **self.activity_snapshot()},
            )
            return ScanResult(
                code=code,
                valid=False,
                url=self.checkout_url(code, region),
                status="timeout",
                message=f"Scan timed out after {SCAN_TIMEOUT_SEC}s — likely rate limited or page stuck loading",
            )

    async def _run_scan_attempts(self, code: int, region: str, headless: bool) -> ScanResult | None:
        if self._fast_scan:
            last: ScanResult | None = None
            for attempt in range(5):
                last = await self._http_probe(code, region)
                if last is None:
                    if attempt < 2:
                        await asyncio.sleep(0.4 * (attempt + 1))
                    continue
                if last.status == "needs_login" and attempt < 2:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                if last.valid:
                    return last
                return last
            return ScanResult(
                code=code,
                valid=False,
                url=self.checkout_url(code, region),
                status="rate_limited",
                message="Temporary response from Battle.net — will recheck automatically",
            )

        last: ScanResult | None = None
        for attempt in range(MAX_SCAN_RETRIES):
            last = await self._browser_scan(code, region, headless)
            if last is None or not _should_retry_scan(last) or attempt == MAX_SCAN_RETRIES - 1:
                break
            await asyncio.sleep(0.4 * (attempt + 1))
        return last

    async def _scan_code_body(self, code: int, region: str, headless: bool) -> ScanResult:
        if not self.session_started_at:
            self.touch_session()

        started_at = datetime.now(timezone.utc).isoformat()
        started_mono = time.monotonic()
        async with self._activity_lock:
            self._active_scans[code] = started_at

        if not self._fast_scan:
            await self._emit(
                "scan_started",
                {
                    "code": code,
                    "started_at": started_at,
                    **self.activity_snapshot(),
                },
            )

        last: ScanResult | None = None
        try:
            last = await self._run_scan_attempts(code, region, headless)
        finally:
            if last and last.status != "needs_login":
                self._scan_durations.append(time.monotonic() - started_mono)
            async with self._activity_lock:
                self._active_scans.pop(code, None)
            if not self._fast_scan:
                await self._emit(
                    "scan_finished",
                    {
                        "code": code,
                        **self.activity_snapshot(),
                    },
                )

        return last or ScanResult(
            code=code,
            valid=False,
            url=self.checkout_url(code, region),
            status="error",
            message="Scan failed with no result — retry this code",
        )

    async def _scan_code_impl(self, code: int, region: str, headless: bool) -> ScanResult:
        if self._fast_scan:
            return await self._scan_code_body(code, region, headless)
        async with self._code_lock(code):
            return await self._scan_code_body(code, region, headless)

    async def pause_auto(self) -> None:
        self._pause_event.clear()
        self.status = ScanStatus.PAUSED
        await self._emit("auto_paused", {})

    async def resume_auto(self) -> None:
        self._pause_event.set()
        self.status = ScanStatus.RUNNING
        await self._emit("auto_resumed", {})

    async def stop_auto(self) -> None:
        await self._stop_running_auto()
        self.status = ScanStatus.IDLE
        await self._emit("auto_stopped", {})

    async def start_auto(
        self,
        start_code: int,
        end_code: int,
        delay_ms: int,
        region: str,
        headless: bool,
        concurrency: int,
        skip_scanned: bool,
        scanned_codes: set[int],
        on_result: Callable[[ScanResult], Any],
        codes_override: list[int] | None = None,
        browser_only: bool = False,
    ) -> bool:
        await self._stop_running_auto()

        self._pause_event.set()
        self.status = ScanStatus.RUNNING
        self._fast_scan = not browser_only
        self._last_progress_emit = 0.0
        workers = max(1, concurrency)
        self._nav_semaphore = asyncio.Semaphore(min(workers, BROWSER_POOL_MAX))
        self._http_semaphore = asyncio.Semaphore(workers)
        if not browser_only:
            await self._dispose_httpx()
        session_started_at = self.touch_session(reset=True)
        if codes_override is not None:
            codes = codes_override
            skipped_count = 0
        else:
            all_codes = list(range(start_code, end_code + 1))
            codes = [code for code in all_codes if not (skip_scanned and code in scanned_codes)]
            skipped_count = len(all_codes) - len(codes)
        self.session_codes_total = len(codes)
        if skipped_count:
            await self._emit("auto_skipped", {"skipped": skipped_count})
        await self._emit(
            "session_started",
            {"session_started_at": session_started_at, **self.activity_snapshot()},
            wait=True,
        )

        if not codes:
            self.status = ScanStatus.IDLE
            await self._emit("auto_finished", {"message": "All codes in range already scanned"})
            return True

        cursor = 0
        cursor_lock = asyncio.Lock()
        progress_lock = asyncio.Lock()
        highest_done = start_code - 1

        async def _emit_progress(code: int, next_code: int) -> None:
            now = time.monotonic()
            if now - self._last_progress_emit < self._progress_emit_interval:
                return
            self._last_progress_emit = now
            await self._emit(
                "progress",
                {
                    "current_code": code,
                    "next_code": next_code,
                    **self.activity_snapshot(),
                },
                wait=False,
            )

        async def worker() -> None:
            nonlocal cursor, highest_done
            while True:
                if self._stop_requested:
                    return
                await self._pause_event.wait()
                if self._stop_requested:
                    return

                async with cursor_lock:
                    if cursor >= len(codes):
                        return
                    code = codes[cursor]
                    cursor += 1

                result = await self.scan_code(code, region=region, headless=headless)

                async def persist_result_task() -> None:
                    callback_result = on_result(result)
                    if asyncio.iscoroutine(callback_result):
                        await callback_result

                asyncio.create_task(persist_result_task())

                async with progress_lock:
                    highest_done = max(highest_done, code)
                    next_code = highest_done + 1
                    self.session_codes_done += 1

                await _emit_progress(code, next_code)

                if delay_ms > 0 and not self._stop_requested:
                    await asyncio.sleep(delay_ms / 1000)

        async def runner() -> None:
            tasks: list[asyncio.Task] = []
            try:
                tasks = [asyncio.create_task(worker()) for _ in range(workers)]
                await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                for task in tasks:
                    task.cancel()
                raise
            finally:
                self._fast_scan = False
                if self.status != ScanStatus.NEEDS_LOGIN:
                    self.status = ScanStatus.IDLE
                async with self._activity_lock:
                    self._active_scans.clear()
                await self._emit("auto_finished", {"last_code": highest_done}, wait=True)

        self._auto_task = asyncio.create_task(runner())
        return True
