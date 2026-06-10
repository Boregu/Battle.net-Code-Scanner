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

from app.library import (
    IMAGES_DIR,
    IMAGE_NONE_MARKER,
    PREREQUISITE_MARKER,
    ALREADY_OWNED_MARKER,
    NOT_ELIGIBLE_MARKER,
    is_usable_price,
    detect_prerequisite_message,
    detect_already_owned_message,
    detect_not_eligible_message,
    ensure_dirs,
    get_product,
)
from app.games import detect_game
from app.catalog_tags import extract_checkout_summary
from app import scan_debug

ROOT = Path(__file__).resolve().parent.parent
AUTH_PATH = ROOT / "data" / "auth.json"
BROWSER_PROFILE_DIR = ROOT / "data" / "browser-profile"

LOGIN_STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
if (!window.chrome) {
  window.chrome = { runtime: {} };
}
"""

REGIONS = {
    "us": "us.checkout.battle.net",
    "eu": "eu.checkout.battle.net",
    "kr": "kr.checkout.battle.net",
    "tw": "tw.checkout.battle.net",
}

REGION_SELECTION_LABELS: dict[str, list[str]] = {
    "us": ["Americas", "Americas & Oceania"],
    "eu": ["Europe"],
    "kr": ["Korea"],
    "tw": ["Taiwan"],
}

ADVANCE_REGION_SELECTION_JS = """
(labels) => {
  if (!/\\/checkout\\/region-selection\\//i.test(window.location.pathname)) {
    return { advanced: false, reason: 'not_region_page' };
  }
  const pick = (text) => labels.find((label) => text.includes(label));
  const select = document.querySelector('select');
  if (select) {
    for (const opt of select.options) {
      const t = (opt.textContent || '').trim();
      if (pick(t)) {
        select.value = opt.value;
        select.dispatchEvent(new Event('input', { bubbles: true }));
        select.dispatchEvent(new Event('change', { bubbles: true }));
        break;
      }
    }
  }
  const nodes = Array.from(
    document.querySelectorAll('button, [role="option"], [role="menuitem"], li, label, span, div')
  );
  for (const label of labels) {
    const hit = nodes.find((el) => (el.textContent || '').trim() === label);
    if (hit) {
      hit.click();
      break;
    }
  }
  const buttons = Array.from(document.querySelectorAll('button, a, [role="button"]'));
  const cont = buttons.find((b) => /^continue$/i.test((b.textContent || '').trim()));
  if (cont) {
    cont.click();
    return { advanced: true, action: 'continue' };
  }
  return { advanced: false, reason: 'no_continue' };
}
"""

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
    if (zeros.length >= 1 && positive.length === 0) return zeros[0];
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
  const allMoneyParts = [];
  for (const label of priceLabels) {
    const labelText = (label.shadowRoot?.textContent || label.textContent || '').replace(/\\s+/g, ' ');
    const parts = labelText.match(/[$€£¥₩]\\s*[\\d][\\d.,]*/g) || [];
    allMoneyParts.push(...parts);
  }
  if (allMoneyParts.length) {
    price = pickPrice(allMoneyParts);
  }

  if (!price) {
    const money = [...new Set(collectMoney(document.body))];
    price = pickPrice(money);
  }

  if (!price) {
    const totalNear = text.match(/TOTAL[\\s\\S]{0,300}?([$€£¥₩]\\s*[\\d][\\d.,]*)/i);
    if (totalNear) {
      price = totalNear[1].replace(/\\s+/g, '');
    }
  }

  if (!price) {
    const findTotalPrice = () => {
      const candidates = Array.from(document.querySelectorAll('span, div, p, strong, h1, h2, h3, h4, meka-price-label, MEKA-PRICE-LABEL'));
      for (const node of candidates) {
        const raw = (node.textContent || '').replace(/\\s+/g, ' ').trim();
        if (!raw || raw.length > 120) continue;
        const inline = raw.match(/^TOTAL\\s*([$€£¥₩][\\d][\\d.,]*)/i);
        if (inline) return inline[1].replace(/\\s+/g, '');
        if (!/^TOTAL\\b/i.test(raw)) continue;
        const parent = node.parentElement;
        if (parent) {
          const block = (parent.textContent || '').replace(/\\s+/g, ' ').trim();
          const blockMatch = block.match(/TOTAL[\\s\\S]{0,160}?([$€£¥₩][\\d][\\d.,]*)/i);
          if (blockMatch) return blockMatch[1].replace(/\\s+/g, '');
        }
      }
      return null;
    };
    price = findTotalPrice();
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
    if (bodyMatch) {
      const val = parseInt(bodyMatch[1].replace(/,/g, ''), 10);
      if (val > 0) return `${bodyMatch[1]} Overwatch Coins`;
    }
    const genericCoin = text.match(/(\\d[\\d,]+)\\s+([A-Za-z][A-Za-z0-9\\u00ae\\u2122\\s]{2,30}Coins)/i);
    if (genericCoin) {
      const val = parseInt(genericCoin[1].replace(/,/g, ''), 10);
      if (val > 0) return `${genericCoin[1]} ${genericCoin[2].replace(/\\s+/g, ' ').trim()}`;
    }
    return null;
  };

  if (!price && /overwatch[\\u00ae\\u2122]?\\s*coins/i.test(lower)) {
    price = pickCoinPrice('Overwatch Coins');
  } else if (!price && /\\bcoins\\b/i.test(lower)) {
    price = pickCoinPrice('Coins');
  } else if (!price && priceLabels.length) {
    const coinPrice = pickCoinPrice('Overwatch Coins');
    if (coinPrice) price = coinPrice;
  }

  if (!price) {
    for (let i = 0; i < lines.length; i += 1) {
      const line = lines[i];
      const inlineTotal = line.match(/^TOTAL\\s*([$€£¥₩][\\d][\\d.,]*)/i);
      if (inlineTotal) {
        price = inlineTotal[1].replace(/\\s+/g, '');
        break;
      }
      if (line.toUpperCase() !== 'TOTAL') continue;
      for (let j = i + 1; j < Math.min(i + 6, lines.length); j += 1) {
        const next = lines[j];
        const cur = next.match(/^([$€£¥₩][\\d][\\d.,]*)/);
        if (cur) {
          price = cur[1].replace(/\\s+/g, '');
          break;
        }
        const coinLine = next.match(/^(\\d[\\d,]+)\\s+(.+Coins)/i);
        if (coinLine) {
          price = `${coinLine[1]} ${coinLine[2].replace(/\\s+/g, ' ').trim()}`;
          break;
        }
        const bare = next.match(/^(\\d[\\d,]+)$/);
        if (bare && /overwatch[\\u00ae\\u2122]?\\s*coins/i.test(lower)) {
          price = `${bare[1]} Overwatch Coins`;
          break;
        }
      }
      if (price) break;
    }
  }

  const pickCurrencyLinePrice = () => {
    const regionHint = text.match(/Change Region\\s*-\\s*(Europe|Americas|Korea|Taiwan|Asia)/i);
    const prefer = regionHint
      ? {
          europe: 'EUR',
          americas: 'USD',
          korea: 'KRW',
          taiwan: 'TWD',
          asia: 'KRW',
        }[regionHint[1].toLowerCase()]
      : null;
    const matches = [
      ...text.matchAll(/(EUR|USD|GBP|CHF|SEK|NOK|DKK|PLN|CZK|TWD|KRW)\\s*-\\s*([\\d][\\d.,]+)/gi),
    ];
    if (!matches.length) return null;
    if (prefer) {
      const hit = matches.find((m) => m[1].toUpperCase() === prefer);
      if (hit) return `${hit[1].toUpperCase()} - ${hit[2]}`;
    }
    for (const code of ['EUR', 'USD', 'GBP']) {
      const hit = matches.find((m) => m[1].toUpperCase() === code);
      if (hit) return `${hit[1].toUpperCase()} - ${hit[2]}`;
    }
    return `${matches[0][1].toUpperCase()} - ${matches[0][2]}`;
  };

  if (!price) {
    price = pickCurrencyLinePrice();
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

  const prereqLine = lines.find((line) => /you need .+ to purchase this product/i.test(line));
  if (lower.includes('first things first') || prereqLine) {
    const prereqMsg = prereqLine || 'Prerequisite required to purchase this product';
    return {
      kind: 'prerequisite',
      name,
      price: null,
      imageUrl,
      genre: null,
      pageText: text.slice(0, 6000),
      requirementsText: '',
      message: prereqMsg,
    };
  }

  if (
    lower.includes('already have access to this product') ||
    /good news[!]?[^\\n]{0,80}already have access/i.test(text)
  ) {
    return {
      kind: 'already_owned',
      name,
      price: null,
      imageUrl,
      genre: null,
      pageText: text.slice(0, 6000),
      requirementsText: '',
      message: 'Scan account already owns this product',
    };
  }

  const ineligibleLine = lines.find((line) => /not eligible to purchase/i.test(line));
  if (lower.includes('not eligible to purchase') || /sorry,? you'?re not eligible/i.test(lower)) {
    const msg = ineligibleLine || 'Scan account not eligible for this product';
    return {
      kind: 'not_eligible',
      name,
      price: null,
      imageUrl,
      genre: null,
      pageText: text.slice(0, 6000),
      requirementsText: '',
      message: msg,
    };
  }

  let kind = 'invalid';
  const onPreloadUrl = /\\/checkout\\/preload\\//i.test(window.location.pathname);
  const onRegionSelection = /\\/checkout\\/region-selection\\//i.test(window.location.pathname);
  if (onRegionSelection && name) {
    kind = 'partial';
  } else if (isCheckout && name && price) {
    kind = 'valid';
  } else if (isCheckout && name) {
    kind = 'partial';
  } else if (name && (onPayUrl || onPreloadUrl || lower.includes('payment information'))) {
    kind = price ? 'valid' : 'partial';
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


def _validate_extract_script_js() -> str:
    body = EXTRACT_SCRIPT.strip()
    return (
        "() => { try { const fn = "
        + body
        + "; if (typeof fn !== 'function') return { ok: false, error: 'EXTRACT_SCRIPT is not a function' }; "
        + "return { ok: true }; } catch (e) { return { ok: false, error: String(e.message || e), "
        + "stack: String(e.stack || '').slice(0, 800) }; } }"
    )


SCANNER_VERSION = "2026-06-04.12"

MAX_SCAN_RETRIES = 4
GENTLE_SCAN_RETRIES = 4
GENTLE_RETRY_BASE_SEC = 1.5
GENTLE_SCAN_TIMEOUT_SEC = 120
GENTLE_BROWSER_GOTO_MS = 25000
GENTLE_PRICE_WAIT_MS = 6000
RATE_LIMIT_PAUSE_SEC = 45
FIX_BROWSER_MAX_CONCURRENCY = 6
SCAN_TIMEOUT_SEC = 45
FAST_SCAN_TIMEOUT_SEC = 120
AUTO_SCAN_MAX_CONCURRENCY = 12
ENRICH_RETRY_ATTEMPTS = 5
ENRICHER_WORKERS = 4
HTTP_PROBE_TIMEOUT_MS = 45000
HTTP_PROBE_RETRIES = 5
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
    r"https?://[^\"']*catalog\.blzstatic\.com[^\"']+\.(?:jpg|jpeg|png|webp)(?:\?[^\"']*)?",
    re.I,
)
HTML_IMG_RE = re.compile(
    r'https?://[^"\']*blzstatic\.com[^"\']+\.(?:jpg|jpeg|png|webp)(?:\?[^"\']*)?',
    re.I,
)
JSON_PRICE_RE = re.compile(r'"price"\s*:\s*"?([\d.]+)"?', re.I)
HTML_MONEY_RE = re.compile(r'[$€£¥₩]\s*[\d][\d.,]*')
CURRENCY_LINE_RE = re.compile(
    r"(EUR|USD|GBP|CHF|SEK|NOK|DKK|PLN|CZK|TWD|KRW)\s*-\s*([\d][\d.,]+)",
    re.I,
)
REGION_CURRENCY = {
    "us": "USD",
    "eu": "EUR",
    "kr": "KRW",
    "tw": "TWD",
}
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


def _image_extension(image_url: str, content_type: str = "") -> str:
    path = image_url.lower().split("?", 1)[0]
    if path.endswith(".png"):
        return ".png"
    if path.endswith(".webp"):
        return ".webp"
    if path.endswith(".gif"):
        return ".gif"
    lower_type = content_type.lower()
    if "png" in lower_type:
        return ".png"
    if "webp" in lower_type:
        return ".webp"
    return ".jpg"


def _local_image_ready(image_path: str | None) -> bool:
    if not (image_path or "").strip():
        return False
    rel = image_path.replace("\\", "/").lstrip("/")
    full = ROOT / rel
    try:
        return full.is_file() and full.stat().st_size >= 256
    except OSError:
        return False


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


def _sanitize_price(price: str | None) -> str | None:
    if not price:
        return None
    trimmed = str(price).strip()
    return trimmed if is_usable_price(trimmed) else None


def _extract_price_from_html(text: str, name: str | None, region: str = "us") -> str | None:
    haystack = f"{name or ''} {text[:12000]}"
    lower = haystack.lower()

    preferred = REGION_CURRENCY.get(region.lower(), "USD")
    currency_matches = list(CURRENCY_LINE_RE.finditer(text[:30000]))
    if currency_matches:
        for match in currency_matches:
            if match.group(1).upper() == preferred:
                return _sanitize_price(f"{match.group(1).upper()} - {match.group(2)}")
        for code in ("EUR", "USD", "GBP"):
            for match in currency_matches:
                if match.group(1).upper() == code:
                    return _sanitize_price(f"{match.group(1).upper()} - {match.group(2)}")
        first = currency_matches[0]
        return _sanitize_price(f"{first.group(1).upper()} - {first.group(2)}")

    coin_match = COIN_BODY_RE.search(haystack)
    if coin_match or "overwatch" in lower and "coins" in lower:
        if coin_match:
            amount = _parse_amount(coin_match.group(1))
            if amount > 0:
                return _sanitize_price(f"{coin_match.group(1)} {coin_match.group(2).strip()}")
        generic = re.search(r"(\d[\d,]+)\s+([A-Za-z][A-Za-z0-9\u00ae\u2122\s]{2,30}Coins)", haystack, re.I)
        if generic and _parse_amount(generic.group(1)) > 0:
            return _sanitize_price(f"{generic.group(1)} {generic.group(2).strip()}")

    total_currency = re.search(
        r"TOTAL[\s\S]{0,300}?([$€£¥₩]\s*[\d][\d.,]*)",
        text[:30000],
        re.I,
    )
    if total_currency:
        return _sanitize_price(total_currency.group(1).replace(" ", ""))

    if name and detect_game(name, haystack):
        game = detect_game(name, haystack)
        if game == "Overwatch":
            total_match = re.search(r"TOTAL[\s\S]{0,80}?(\d[\d,]+)", haystack, re.I)
            if total_match and _parse_amount(total_match.group(1)) > 0:
                return _sanitize_price(f"{total_match.group(1)} Overwatch Coins")

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
                return _sanitize_price(f"{positive[0][1]} (was {positive[-1][1]})")
            return _sanitize_price(positive[0][1])
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
    notes = (result.raw_notes or "").lower()
    if ALREADY_OWNED_MARKER.lower() in notes or PREREQUISITE_MARKER.lower() in notes:
        return False
    if NOT_ELIGIBLE_MARKER.lower() in notes or "not eligible to purchase" in notes:
        return False
    if detect_already_owned_message(result.message) or detect_prerequisite_message(result.message):
        return False
    if detect_not_eligible_message(result.message):
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


def _account_gated_scan_result(
    code: int,
    *,
    name: str | None,
    message: str,
    marker: str,
    image_url: str | None,
    image_path: str | None,
    url: str | None,
    game: str | None,
) -> ScanResult:
    raw_notes = marker if message == marker else f"{marker}\n{message}"
    return ScanResult(
        code=code,
        valid=True,
        name=name,
        price=None,
        image_url=image_url,
        image_path=image_path,
        url=url,
        status="valid",
        message=message,
        game=game,
        raw_notes=raw_notes,
    )


def _owned_scan_result(
    code: int,
    *,
    name: str | None,
    message: str,
    image_url: str | None,
    image_path: str | None,
    url: str | None,
    game: str | None,
) -> ScanResult:
    return _account_gated_scan_result(
        code,
        name=name,
        message=message,
        marker=ALREADY_OWNED_MARKER,
        image_url=image_url,
        image_path=image_path,
        url=url,
        game=game,
    )


def _not_eligible_scan_result(
    code: int,
    *,
    name: str | None,
    message: str,
    image_url: str | None,
    image_path: str | None,
    url: str | None,
    game: str | None,
) -> ScanResult:
    result = _account_gated_scan_result(
        code,
        name=name,
        message=message,
        marker=NOT_ELIGIBLE_MARKER,
        image_url=image_url,
        image_path=image_path,
        url=url,
        game=game,
    )
    result.status = "not_eligible"
    return result


class BattleNetScanner:
    def __init__(self) -> None:
        ensure_dirs()
        self._playwright = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._httpx: httpx.AsyncClient | None = None
        self._browser_lock = asyncio.Lock()
        self._nav_semaphore = asyncio.Semaphore(BROWSER_POOL_MAX)
        self._enrich_semaphore = asyncio.Semaphore(ENRICHER_WORKERS)
        self._http_semaphore = asyncio.Semaphore(HTTP_PROBE_CONCURRENCY)
        self._enrich_queue: asyncio.Queue | None = None
        self._enrich_workers: list[asyncio.Task] = []
        self._on_enriched: Callable[[ScanResult], Any] | None = None
        self._enrich_region = "us"
        self._enrich_headless = True
        self._enrich_pending = 0
        self._code_locks: dict[int, asyncio.Lock] = {}
        self._active_headless: bool | None = None
        self._page_pool: asyncio.Queue[Page] = asyncio.Queue()
        self._pool_size = 0
        self._login_page: Page | None = None
        self._login_browser_channel: str | None = None
        self.status = ScanStatus.IDLE
        self._auto_task: asyncio.Task | None = None
        self._pause_event = asyncio.Event()
        self._pause_event.set()
        self._stop_requested = False
        self._on_event: Callable[[str, dict[str, Any]], Any] | None = None
        self.session_started_at: str | None = None
        self._active_scans: dict[int, str] = {}
        self._active_scan_phases: dict[int, str] = {}
        self._activity_lock = asyncio.Lock()
        self.session_codes_done = 0
        self.session_codes_total = 0
        self._scan_durations: deque[float] = deque(maxlen=100)
        self._fast_scan = False
        self._gentle_scan = False
        self._enrich_only_codes: set[int] = set()
        self._use_chrome_browser = False
        self._active_region = "us"
        self._progress_emit_interval = 0.2
        self._last_progress_emit = 0.0
        self._extract_script_ok: bool | None = None
        self._extract_script_error: str | None = None
        self._extract_validate_task: asyncio.Task | None = None

    def extract_script_status(self) -> dict[str, Any]:
        return {
            "ok": self._extract_script_ok,
            "error": self._extract_script_error,
            "log_path": str(scan_debug.LOG_PATH),
        }

    async def validate_extract_script(self, *, force: bool = False) -> bool:
        if self._extract_script_ok is True and not force:
            return True
        if self._extract_validate_task and not self._extract_validate_task.done() and not force:
            return await self._extract_validate_task
        self._extract_validate_task = asyncio.create_task(
            self._validate_extract_script_impl(force=force)
        )
        return await self._extract_validate_task

    def _schedule_extract_validation(self) -> None:
        if self._extract_validate_task and not self._extract_validate_task.done():
            return
        self._extract_validate_task = asyncio.create_task(self._validate_extract_script_impl())

    async def _validate_extract_script_impl(self, *, force: bool = False) -> bool:
        if self._extract_script_ok is True and not force:
            return True

        node_ok, node_err = scan_debug.validate_js_with_node(EXTRACT_SCRIPT)
        if node_ok is False:
            self._extract_script_ok = False
            self._extract_script_error = node_err
            entry = scan_debug.log(
                "critical",
                "extract_script_invalid",
                source="node",
                error=node_err,
                hint=scan_debug._syntax_hint(node_err or ""),
            )
            await self._emit("scan_debug", entry)
            return False
        if node_ok is True:
            scan_debug.log("info", "extract_script_ok", source="node")

        try:
            page = await self._borrow_page(headless=True)
            try:
                await page.goto("about:blank", wait_until="commit", timeout=5000)
                result = await page.evaluate(_validate_extract_script_js())
            finally:
                await self._return_page(page)
        except Exception as exc:
            detail = str(exc).split("\n", 1)[0][:300]
            scan_debug.log("warn", "extract_script_check_skipped", error=detail)
            if node_ok is True:
                self._extract_script_ok = True
                self._extract_script_error = None
                return True
            return False

        if result.get("ok"):
            self._extract_script_ok = True
            self._extract_script_error = None
            scan_debug.log("info", "extract_script_ok", source="browser")
            return True

        err = str(result.get("error") or "EXTRACT_SCRIPT validation failed")
        self._extract_script_ok = False
        self._extract_script_error = err
        entry = scan_debug.log(
            "critical",
            "extract_script_invalid",
            source="browser",
            error=err,
            stack=result.get("stack"),
            hint=scan_debug._syntax_hint(err),
        )
        await self._emit("scan_debug", entry)
        return False

    async def _run_extract(
        self,
        page: Page,
        *,
        code: int | None = None,
        url: str = "",
        phase: str = "scan",
        log_result: bool = True,
    ) -> dict[str, Any]:
        if self._extract_script_ok is False:
            raise RuntimeError(
                self._extract_script_error
                or "EXTRACT_SCRIPT is broken — fix app/scanner.py and restart"
            )
        try:
            extracted = await page.evaluate(EXTRACT_SCRIPT)
        except Exception as exc:
            detail = str(exc).split("\n", 1)[0][:500]
            entry = scan_debug.log_extract_error(code, phase, url or page.url or "", detail)
            await self._emit("scan_debug", entry)
            raise
        if log_result:
            scan_debug.log_extract_result(code, phase, url or page.url or "", extracted)
        return extracted

    async def _apply_browser_html_fallbacks(
        self, page: Page, extracted: dict[str, Any]
    ) -> dict[str, Any]:
        name = extracted.get("name")
        need_price = not extracted.get("price") and name
        need_img = not extracted.get("imageUrl")
        need_genre = not extracted.get("genre")
        if not need_price and not need_img and not need_genre:
            return extracted
        try:
            html = await page.content()
        except Exception:
            return extracted
        if need_price:
            fallback_price = _extract_price_from_html(html, name, self._active_region)
            if fallback_price:
                extracted["price"] = fallback_price
                if extracted.get("kind") in ("invalid", "partial"):
                    extracted["kind"] = "valid"
        if need_img:
            fallback_img = _extract_image_url_from_html(html)
            if fallback_img:
                extracted["imageUrl"] = fallback_img
        if need_genre:
            fallback_genre = _extract_genre_from_html(html)
            if fallback_genre:
                extracted["genre"] = fallback_genre
        return extracted

    async def _run_extract_with_price_wait(
        self,
        page: Page,
        *,
        code: int | None = None,
        url: str = "",
        phase: str = "scan",
    ) -> dict[str, Any]:
        page_url = page.url or url or ""
        if "/checkout/region-selection/" in page_url:
            await self._advance_region_selection(page, self._active_region)
            await self._ensure_checkout_price_ready(
                page,
                self._active_region,
                pay_wait=20.0,
                price_wait_ms=8000,
            )
            page_url = page.url or page_url
        extracted = await self._run_extract(
            page, code=code, url=page_url, phase=phase, log_result=False
        )
        if extracted.get("kind") in ("already_owned", "not_eligible", "prerequisite"):
            scan_debug.log_extract_result(code, phase, page_url, extracted)
            return extracted
        on_pay = "/checkout/pay/" in page_url or "/checkout/pay/" in (page.url or "")
        if not on_pay and "/checkout/region-selection/" not in (page.url or ""):
            extracted = await self._apply_browser_html_fallbacks(page, extracted)
            scan_debug.log_extract_result(code, phase, page_url, extracted)
            return extracted

        if not extracted.get("price"):
            extracted = await self._apply_browser_html_fallbacks(page, extracted)

        if not extracted.get("price"):
            for attempt in range(4):
                await self._wait_for_price_labels(page, timeout_ms=5000)
                await asyncio.sleep(0.25 * (attempt + 1))
                extracted = await self._run_extract(
                    page,
                    code=code,
                    url=page.url or page_url,
                    phase=f"{phase}_price_retry_{attempt + 1}",
                    log_result=False,
                )
                if extracted.get("price"):
                    break
                extracted = await self._apply_browser_html_fallbacks(page, extracted)
                if extracted.get("price"):
                    break

        extracted = await self._apply_browser_html_fallbacks(page, extracted)
        scan_debug.log_extract_result(code, phase, page.url or page_url, extracted)
        return extracted

    async def _set_scan_phase(self, code: int, phase: str) -> None:
        async with self._activity_lock:
            self._active_scan_phases[code] = phase
        scan_debug.log("info", "scan_phase", code=code, phase=phase)
        if not self._fast_scan:
            await self._emit(
                "scan_phase",
                {"code": code, "phase": phase, **self.activity_snapshot()},
                wait=False,
            )

    async def _clear_scan_phase(self, code: int) -> None:
        async with self._activity_lock:
            self._active_scan_phases.pop(code, None)

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
            "active_scan_phases": {str(code): phase for code, phase in self._active_scan_phases.items()},
            "session_codes_done": self.session_codes_done,
            "session_codes_total": self.session_codes_total,
            "eta_seconds": eta_seconds,
            "avg_seconds_per_scan": round(avg_seconds, 2) if avg_seconds else None,
            "scans_per_second": scans_per_second,
            "enrich_pending": self._enrich_pending,
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

    def _httpx_client(self, max_connections: int = HTTP_PROBE_CONCURRENCY) -> httpx.AsyncClient:
        pool = max(4, max_connections)
        return httpx.AsyncClient(
            cookies=self._load_auth_cookies(),
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            follow_redirects=True,
            max_redirects=HTTP_PROBE_MAX_REDIRECTS,
            timeout=httpx.Timeout(HTTP_PROBE_TIMEOUT_MS / 1000, connect=10.0),
            limits=httpx.Limits(max_connections=pool, max_keepalive_connections=pool),
        )

    async def _ensure_httpx(self, *, pool_size: int | None = None) -> httpx.AsyncClient:
        pool = max(4, pool_size or HTTP_PROBE_CONCURRENCY)
        if self._httpx is None:
            self._httpx = self._httpx_client(max_connections=pool)
        return self._httpx

    async def _dispose_httpx(self) -> None:
        if self._httpx:
            await self._httpx.aclose()
            self._httpx = None

    async def _http_get(self, url: str) -> tuple[int, str, str, dict[str, str]] | None:
        last_error: Exception | None = None
        async with self._http_semaphore:
            client = await self._ensure_httpx()
            for attempt in range(HTTP_PROBE_RETRIES):
                try:
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
        self._login_browser_channel = None
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
            launch_args = [
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ]
            if self._gentle_scan or self._use_chrome_browser:
                launch_args.extend(["--disable-gpu"])
            else:
                launch_args.extend(
                    [
                        "--disable-gpu",
                        "--disable-extensions",
                        "--disable-background-networking",
                    ]
                )
            browser = None
            if self._gentle_scan or self._use_chrome_browser:
                for channel in ("chrome", "msedge", None):
                    try:
                        kwargs: dict[str, Any] = {
                            "headless": headless,
                            "args": launch_args,
                            "ignore_default_args": ["--enable-automation"],
                        }
                        if channel:
                            kwargs["channel"] = channel
                        browser = await self._playwright.chromium.launch(**kwargs)
                        break
                    except Exception:
                        continue
            if browser is None:
                browser = await self._playwright.chromium.launch(
                    headless=headless,
                    args=launch_args,
                )
            self._browser = browser
            context_kwargs: dict[str, Any] = {
                "viewport": {"width": 1280, "height": 800},
                "user_agent": USER_AGENT,
            }
            if AUTH_PATH.exists():
                context_kwargs["storage_state"] = str(AUTH_PATH)

            self._context = await self._browser.new_context(**context_kwargs)
            if self._gentle_scan or self._use_chrome_browser:
                await self._context.add_init_script(LOGIN_STEALTH_INIT)
            self._active_headless = headless
            self._schedule_extract_validation()
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

    async def _launch_login_context(self) -> tuple[BrowserContext, str]:
        BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        await self._ensure_playwright()
        base_kwargs: dict[str, Any] = {
            "user_data_dir": str(BROWSER_PROFILE_DIR),
            "headless": False,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
            "ignore_default_args": ["--enable-automation"],
            "viewport": {"width": 1400, "height": 900},
        }
        last_error: Exception | None = None
        for channel in ("chrome", "msedge", None):
            label = channel or "chromium"
            try:
                kwargs = dict(base_kwargs)
                if channel:
                    kwargs["channel"] = channel
                context = await self._playwright.chromium.launch_persistent_context(**kwargs)
                await context.add_init_script(LOGIN_STEALTH_INIT)
                return context, label
            except Exception as exc:
                last_error = exc
        raise RuntimeError(
            "Could not launch a login browser. Install Google Chrome, then try Open Login again."
        ) from last_error

    async def open_login_browser(self) -> str:
        async with self._browser_lock:
            await self._close_browser()
            self._context, channel = await self._launch_login_context()
            self._browser = None
            self._login_browser_channel = channel
            self._login_page = self._context.pages[0] if self._context.pages else await self._context.new_page()
            await self._login_page.goto(
                "https://us.checkout.battle.net/shop/en/checkout/buy/64313",
                wait_until="domcontentloaded",
            )
            self._active_headless = False
            self.status = ScanStatus.NEEDS_LOGIN
            await self._emit(
                "login_browser_opened",
                {
                    "message": "Log in, then click Save Session.",
                    "browser": channel,
                },
            )
            return channel

    async def save_login_session(self) -> bool:
        async with self._browser_lock:
            if not self._context:
                return False
            await self._context.storage_state(path=str(AUTH_PATH))
            await self._dispose_httpx()
            await self._close_browser()
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
        price = _extract_price_from_html(text, name, self._active_region)
        image_url = _extract_image_url_from_html(text)
        ready = is_usable_price(price) or bool(image_url)
        return ScanResult(
            code=code,
            valid=True,
            name=name,
            price=price,
            image_url=image_url,
            url=final_url,
            status="valid" if (name and ready) else "partial",
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
        price = _extract_price_from_html(text, name, self._active_region)
        image_url = _extract_image_url_from_html(text)
        ready = is_usable_price(price) or bool(image_url)
        return ScanResult(
            code=code,
            valid=True,
            name=name,
            price=price,
            image_url=image_url,
            url=final_url,
            status="valid" if ready else "partial",
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
        await self._set_scan_phase(code, "http")
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
                  const bodyText = document.body?.innerText || '';
                  const onCheckout = /\/checkout\/(pay|preload)\//i.test(window.location.pathname);
                  if (lower.includes('nothing here') || lower.includes('log in or sign up')) return true;
                  if (onCheckout && /TOTAL[\s\S]{0,300}[$€£¥₩][\d]/i.test(bodyText)) return true;
                  if (onCheckout && /(?:EUR|USD|GBP|CHF|SEK|NOK|DKK|PLN|CZK|TWD|KRW)\s*-\s*[\d]/i.test(bodyText)) return true;
                  if (onCheckout && lower.includes('payment information') && /[$€£¥₩][\d]/.test(bodyText)) return true;
                  const labels = Array.from(document.querySelectorAll('meka-price-label, MEKA-PRICE-LABEL'));
                  if (!labels.length) return false;
                  const usesCoins = /overwatch[\u00ae\u2122]?\s*coins/i.test(lower) || /\bcoins\b/i.test(lower);
                  if (usesCoins) {
                    if (!onCheckout && labels.length < 6) return false;
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
                    return countMax >= 2 || max >= 2500 || (onCheckout && countMax >= 1 && max >= 100);
                  }
                  for (const el of labels) {
                    const text = (el.shadowRoot?.textContent || el.textContent || '').trim();
                    const match = text.match(/[$€£¥₩]\s*[\d][\d.,]*/);
                    if (match && parseAmount(match[0]) > 0) return true;
                    if (/^\d[\d,]+$/.test(text) && parseInt(text.replace(/,/g, ''), 10) > 0) return true;
                  }
                  return false;
                }""",
                timeout=timeout_ms,
                polling=100,
            )
        except Exception:
            pass

    async def _advance_region_selection(self, page: Page, region: str) -> bool:
        if "/checkout/region-selection/" not in (page.url or ""):
            return False
        labels = REGION_SELECTION_LABELS.get(region.lower(), REGION_SELECTION_LABELS["us"])
        try:
            result = await page.evaluate(ADVANCE_REGION_SELECTION_JS, labels)
            if not result or not result.get("advanced"):
                try:
                    continue_btn = page.get_by_role("button", name=re.compile(r"^Continue$", re.I))
                    if await continue_btn.count():
                        await continue_btn.first.click(timeout=5000)
                    else:
                        return False
                except Exception:
                    return False
            try:
                await page.wait_for_function(
                    "() => !/\\/checkout\\/region-selection\\//i.test(window.location.pathname)",
                    timeout=25000,
                )
            except Exception:
                await asyncio.sleep(2)
            return "/checkout/region-selection/" not in (page.url or "")
        except Exception:
            return False

    async def _ensure_checkout_price_ready(
        self,
        page: Page,
        region: str,
        *,
        pay_wait: float = 28.0,
        price_wait_ms: int = 8000,
    ) -> None:
        for _ in range(3):
            if await self._advance_region_selection(page, region):
                await asyncio.sleep(0.5)
            if "/checkout/region-selection/" in (page.url or ""):
                await asyncio.sleep(0.75)
                continue
            break
        if "/checkout/pay/" not in (page.url or ""):
            await self._wait_for_pay_url(page, timeout_sec=pay_wait)
        if "/checkout/pay/" in (page.url or ""):
            await self._wait_for_price_labels(page, timeout_ms=price_wait_ms)

    async def _wait_for_pay_url(self, page: Page, *, timeout_sec: float = 28.0) -> bool:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            url = page.url or ""
            if "/checkout/pay/" in url:
                return True
            if "/checkout/region-selection/" in url:
                await self._advance_region_selection(page, self._active_region)
            if "nothing here" in (await page.evaluate("() => (document.body?.innerText || '').toLowerCase()") or ""):
                return False
            await asyncio.sleep(0.15)
        return "/checkout/pay/" in (page.url or "")

    async def _poll_checkout_ready(self, page: Page) -> None:
        if page.url.startswith("chrome-error://"):
            return

        deadline = time.monotonic() + (FAST_POLL_MAX_SEC if self._fast_scan else 22.0)
        interval = FAST_POLL_INTERVAL_SEC if self._fast_scan else 0.15

        while time.monotonic() < deadline:
            try:
                ready = await page.evaluate(
                    """() => {
                      const text = document.body?.innerText || '';
                      const lower = text.toLowerCase();
                      if (!text.length) return false;
                      if (lower.includes('nothing here')) return true;
                      if (lower.includes('log in or sign up')) return true;
                      if (lower.includes('first things first') || /you need .+ to purchase this product/i.test(text)) return true;
                      if (lower.includes('already have access to this product')) return true;
                      if (/good news[!]?[^\\n]{0,80}already have access/i.test(text)) return true;
                      if (lower.includes('not eligible to purchase') || /sorry,? you'?re not eligible/i.test(lower)) return true;

                      const hasMoneyLabel = () => {
                        const labels = Array.from(document.querySelectorAll('meka-price-label, MEKA-PRICE-LABEL'));
                        for (const el of labels) {
                          const t = (el.shadowRoot?.textContent || el.textContent || '').replace(/\\s+/g, ' ').trim();
                          const m = t.match(/^[$€£¥₩]\\s*([\\d][\\d.,]*)/);
                          if (m && parseFloat(m[1].replace(/[^0-9.]/g, '')) > 0) return true;
                          if (/^\\d[\\d,]+$/.test(t) && parseInt(t.replace(/,/g, ''), 10) > 0) return true;
                        }
                        return false;
                      };

                      if (/\\/checkout\\/pay\\//i.test(window.location.pathname)) {
                        if (hasMoneyLabel()) return true;
                        if (/TOTAL[\\s\\S]{0,300}[$€£¥₩][\\d]/i.test(text)) return true;
                        if (/(?:EUR|USD|GBP|CHF|SEK|NOK|DKK|PLN|CZK|TWD|KRW)\\s*-\\s*[\\d]/i.test(text)) return true;
                        if (lower.includes('you are purchasing') && /[$€£¥₩]\\s*\\d/.test(text)) return true;
                        if (lower.includes('you are purchasing') && lower.includes('payment information')) return true;
                        return false;
                      }

                      if (lower.includes('you are purchasing') && /[$€£¥₩]\\s*\\d/.test(text)) return true;
                      if (lower.includes('you are purchasing') && hasMoneyLabel()) return true;
                      if (/\\/checkout\\/preload\\//i.test(window.location.pathname)) {
                        if (lower.includes('total') && (
                          lower.includes('product summary') ||
                          document.querySelector('meka-price-label, MEKA-PRICE-LABEL, img[src*="blzstatic"]')
                        )) return true;
                        if (lower.includes('you are purchasing') && document.querySelector('img[src*="blzstatic"]')) return true;
                        return false;
                      }
                      if (/^buy\\s/i.test(document.title || '') && text.length > 200) return true;
                      return false;
                    }"""
                )
                if ready:
                    return
            except Exception:
                pass
            await asyncio.sleep(interval)

        if not self._fast_scan:
            await self._wait_for_price_labels(page, timeout_ms=5000)

    async def _ensure_enriched_result(
        self,
        code: int,
        region: str,
        headless: bool,
        base: ScanResult,
    ) -> ScanResult:
        if not self._needs_browser_enrich(base):
            return base

        last = base
        for attempt in range(ENRICH_RETRY_ATTEMPTS):
            enriched = await self._browser_enrich_valid(code, region, headless, last)
            if enriched:
                last = enriched
            if not self._needs_browser_enrich(last):
                return last
            if attempt + 1 < ENRICH_RETRY_ATTEMPTS:
                await asyncio.sleep(0.8 * (attempt + 1))

        if self._needs_browser_enrich(last):
            browser = await self._browser_scan(code, region, headless)
            if browser and browser.valid:
                last = browser
                if self._needs_browser_enrich(last):
                    enriched = await self._browser_enrich_valid(code, region, headless, last)
                    if enriched:
                        last = enriched

        if self._needs_browser_enrich(last) and self._enrich_queue is not None:
            self._queue_background_enrich(code, last)

        return last

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
                    old_fast = self._fast_scan
                    self._fast_scan = False
                    gentle = self._gentle_scan
                    goto_ms = GENTLE_BROWSER_GOTO_MS if gentle else 20000
                    pay_wait = 12.0 if gentle else 28.0
                    price_wait = GENTLE_PRICE_WAIT_MS if gentle else 18000
                    try:
                        await page.goto(target, wait_until="domcontentloaded", timeout=goto_ms)
                    except Exception as exc:
                        detail = str(exc).split("\n", 1)[0][:200].lower()
                        if "too_many_redirects" in detail or "err_too_many_redirects" in detail:
                            return base
                        if "net::" in detail or "timeout" in detail:
                            return base
                        raise

                    await asyncio.sleep(1.0)
                    await self._poll_checkout_ready(page)
                    await self._ensure_checkout_price_ready(
                        page,
                        region,
                        pay_wait=pay_wait,
                        price_wait_ms=price_wait,
                    )

                    extracted = await self._run_extract_with_price_wait(
                        page,
                        code=code,
                        url=page.url or target,
                        phase="enrich",
                    )
                    if (
                        extracted.get("kind") in ("valid", "partial")
                        and not extracted.get("price")
                        and "/checkout/pay/" in (page.url or "")
                    ):
                        await self._wait_for_price_labels(page, timeout_ms=12000)
                        extracted = await self._run_extract_with_price_wait(
                            page,
                            code=code,
                            url=page.url or target,
                            phase="enrich_price_retry",
                        )
                    kind = extracted.get("kind")
                    if kind == "empty":
                        page_text = extracted.get("pageText") or ""
                        return ScanResult(
                            code=code,
                            valid=False,
                            name=extracted.get("name") or base.name,
                            url=page.url or base.url,
                            status="empty",
                            message=extracted.get("message") or _empty_product_message(page_text),
                        )
                    if kind in ("login", "rate_limited"):
                        return base

                    if kind == "prerequisite":
                        name = extracted.get("name") or base.name
                        msg = extracted.get("message") or "Prerequisite required to purchase this product"
                        image_url = extracted.get("imageUrl") or base.image_url
                        image_path = base.image_path
                        if image_url and not _local_image_ready(image_path):
                            image_path = await self._download_image(page, image_url, code)
                        raw_notes = f"{PREREQUISITE_MARKER}\n{msg}"
                        game = detect_game(name, extracted.get("pageText") or "")
                        return ScanResult(
                            code=code,
                            valid=True,
                            name=name,
                            price=None,
                            image_url=image_url,
                            image_path=image_path,
                            url=page.url or base.url,
                            status="valid",
                            message=msg,
                            game=game or base.game,
                            raw_notes=raw_notes,
                        )

                    if kind == "already_owned":
                        name = extracted.get("name") or base.name
                        msg = extracted.get("message") or ALREADY_OWNED_MARKER
                        image_url = extracted.get("imageUrl") or base.image_url
                        image_path = base.image_path
                        if image_url and not _local_image_ready(image_path):
                            image_path = await self._download_image(page, image_url, code)
                        game = detect_game(name, extracted.get("pageText") or "")
                        return _owned_scan_result(
                            code,
                            name=name,
                            message=msg,
                            image_url=image_url,
                            image_path=image_path,
                            url=page.url or base.url,
                            game=game or base.game,
                        )

                    if kind == "not_eligible":
                        name = extracted.get("name") or base.name
                        msg = extracted.get("message") or NOT_ELIGIBLE_MARKER
                        image_url = extracted.get("imageUrl") or base.image_url
                        image_path = base.image_path
                        if image_url and not _local_image_ready(image_path):
                            image_path = await self._download_image(page, image_url, code)
                        game = detect_game(name, extracted.get("pageText") or "")
                        return _not_eligible_scan_result(
                            code,
                            name=name,
                            message=msg,
                            image_url=image_url,
                            image_path=image_path,
                            url=page.url or base.url,
                            game=game or base.game,
                        )

                    name = extracted.get("name") or base.name
                    page_url = page.url or base.url or ""
                    if kind == "invalid" and name and (
                        "/checkout/preload/" in page_url or "/checkout/pay/" in page_url
                    ):
                        kind = "valid"

                    if kind not in ("valid", "partial") and not extracted.get("price") and not extracted.get("imageUrl"):
                        if "/checkout/preload/" in page_url:
                            await self._wait_for_price_labels(page, timeout_ms=15000)
                            extracted = await self._run_extract_with_price_wait(
                                page,
                                code=code,
                                url=page.url or target,
                                phase="enrich_retry",
                            )
                            kind = extracted.get("kind")
                            name = extracted.get("name") or base.name
                            if kind == "invalid" and name:
                                kind = "valid"

                    if kind == "empty":
                        page_text = extracted.get("pageText") or ""
                        return ScanResult(
                            code=code,
                            valid=False,
                            name=extracted.get("name") or base.name,
                            url=page.url or base.url,
                            status="empty",
                            message=extracted.get("message") or _empty_product_message(page_text),
                        )
                    if kind in ("login", "rate_limited"):
                        return base
                    if kind not in ("valid", "partial"):
                        return base

                    price = _sanitize_price(extracted.get("price") or base.price)
                    image_url = extracted.get("imageUrl") or base.image_url
                    image_path = base.image_path
                    if image_url and not _local_image_ready(image_path):
                        image_path = await self._download_image(page, image_url, code)

                    page_text = extracted.get("pageText") or ""
                    requirements_text = extracted.get("requirementsText") or ""
                    genre = extracted.get("genre")
                    notes_parts: list[str] = []
                    summary = extract_checkout_summary(page_text)
                    if summary:
                        notes_parts.append(f"Summary: {summary}")
                    if requirements_text:
                        notes_parts.append(requirements_text)
                    if genre:
                        notes_parts.append(f"Genre: {genre}")
                    raw_notes = "\n".join(notes_parts) or None
                    if not image_url and not image_path:
                        raw_notes = (
                            f"{raw_notes}\n{IMAGE_NONE_MARKER}".strip()
                            if raw_notes
                            else IMAGE_NONE_MARKER
                        )
                    game = detect_game(name, f"{page_text}\n{requirements_text}", genre=genre)

                    result = ScanResult(
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
                    if self._needs_browser_enrich(result):
                        result.status = "partial"
                    return result
                except Exception:
                    return base
                finally:
                    self._fast_scan = old_fast
                    await self._return_page(page)

    async def _start_enrich_workers(
        self,
        region: str,
        headless: bool,
        on_enriched: Callable[[ScanResult], Any] | None,
    ) -> None:
        await self._stop_enrich_workers(wait=False)
        self._on_enriched = on_enriched
        self._enrich_region = region
        self._enrich_headless = headless
        self._enrich_pending = 0
        self._enrich_queue = asyncio.Queue(maxsize=800)
        self._enrich_workers = [
            asyncio.create_task(self._enrich_worker()) for _ in range(ENRICHER_WORKERS)
        ]

    async def _stop_enrich_workers(self, *, wait: bool = True) -> None:
        queue = self._enrich_queue
        workers = self._enrich_workers
        self._enrich_queue = None
        self._enrich_workers = []
        self._on_enriched = None
        self._enrich_pending = 0
        if not queue or not workers:
            return
        for _ in workers:
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        if wait:
            await asyncio.gather(*workers, return_exceptions=True)

    async def _enrich_worker(self) -> None:
        queue = self._enrich_queue
        if queue is None:
            return
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                code, base = item
                enriched = await self._browser_enrich_valid(
                    code,
                    self._enrich_region,
                    self._enrich_headless,
                    base,
                )
                if self._on_enriched and enriched.valid:
                    callback = self._on_enriched(enriched)
                    if asyncio.iscoroutine(callback):
                        await callback
            finally:
                queue.task_done()
                self._enrich_pending = max(0, self._enrich_pending - 1)

    def _queue_background_enrich(self, code: int, base: ScanResult) -> None:
        if self._enrich_queue is None:
            return
        try:
            self._enrich_queue.put_nowait((code, base))
            self._enrich_pending += 1
        except asyncio.QueueFull:
            pass

    async def _browser_scan(self, code: int, region: str, headless: bool) -> ScanResult:
        await self._set_scan_phase(code, "browser")
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
            client = await self._ensure_httpx()
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
            ext = _image_extension(absolute, content_type)
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
            result.price = _extract_price_from_html(text, name, self._active_region)
        result.price = _sanitize_price(result.price)

        if not result.image_url:
            result.image_url = _extract_image_url_from_html(text)

        if result.image_url and not _local_image_ready(result.image_path):
            result.image_path = await self._download_image_http(
                result.image_url, result.code, referer=final_url
            )

        genre = _extract_genre_from_html(text)
        requirements_bits: list[str] = []
        if genre:
            requirements_bits.append(f"Genre: {genre}")
        if requirements_bits:
            result.raw_notes = "\n".join(requirements_bits)

        if not result.game and name:
            result.game = detect_game(name, text[:6000], genre=genre)

        if result.valid and result.name and result.status == "partial":
            if is_usable_price(result.price) or _local_image_ready(result.image_path):
                result.status = "valid"
                result.message = None

        return self._apply_not_eligible_from_text(
            self._apply_already_owned_from_text(
                self._apply_prerequisite_from_text(result, text),
                text,
            ),
            text,
        )

    def _apply_prerequisite_from_text(self, result: ScanResult, text: str) -> ScanResult:
        msg = detect_prerequisite_message(text)
        if not msg:
            return result
        notes = (result.raw_notes or "").strip()
        if PREREQUISITE_MARKER.lower() not in notes.lower():
            notes = f"{PREREQUISITE_MARKER}\n{msg}" + (f"\n{notes}" if notes else "")
        result.valid = True
        result.status = "valid"
        result.message = msg
        result.raw_notes = notes
        return result

    def _apply_already_owned_from_text(self, result: ScanResult, text: str) -> ScanResult:
        msg = detect_already_owned_message(text)
        if not msg:
            return result
        notes = (result.raw_notes or "").strip()
        if ALREADY_OWNED_MARKER.lower() not in notes.lower():
            notes = ALREADY_OWNED_MARKER + (f"\n{notes}" if notes else "")
        result.valid = True
        result.status = "valid"
        result.message = msg
        result.raw_notes = notes
        result.price = None
        return result

    def _apply_not_eligible_from_text(self, result: ScanResult, text: str) -> ScanResult:
        msg = detect_not_eligible_message(text)
        if not msg:
            return result
        notes = (result.raw_notes or "").strip()
        if NOT_ELIGIBLE_MARKER.lower() not in notes.lower():
            head = NOT_ELIGIBLE_MARKER if msg == NOT_ELIGIBLE_MARKER else f"{NOT_ELIGIBLE_MARKER}\n{msg}"
            notes = head + (f"\n{notes}" if notes else "")
        result.valid = True
        result.status = "valid"
        result.message = msg
        result.raw_notes = notes
        result.price = None
        return result

    def _needs_browser_enrich(self, result: ScanResult) -> bool:
        if not result.valid:
            return False
        notes = (result.raw_notes or "").lower()
        if PREREQUISITE_MARKER.lower() in notes:
            return False
        if ALREADY_OWNED_MARKER.lower() in notes:
            return False
        if NOT_ELIGIBLE_MARKER.lower() in notes or "not eligible to purchase" in notes:
            return False
        if detect_prerequisite_message(result.message):
            return False
        if detect_already_owned_message(result.message):
            return False
        if detect_not_eligible_message(result.message):
            return False
        if not (result.name or "").strip():
            return True
        if not is_usable_price(result.price):
            return True
        if not (result.game or "").strip():
            return True
        notes = (result.raw_notes or "").lower()
        if "image: none" in notes:
            return False
        if not _local_image_ready(result.image_path):
            return True
        return False

    async def _download_image(self, page: Page, image_url: str, code: int) -> str | None:
        saved = await self._download_image_http(image_url, code, referer=page.url or None)
        if saved:
            return saved
        if not image_url:
            return None
        absolute = urljoin(page.url or "https://eu.checkout.battle.net/", image_url)
        try:
            response = await page.request.get(absolute)
            if not response.ok:
                return None
            body = await response.body()
            if len(body) < 256:
                return None
            content_type = response.headers.get("content-type", "")
            ext = _image_extension(absolute, content_type)
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
            goto_timeout = 12000 if self._fast_scan else (GENTLE_BROWSER_GOTO_MS if self._gentle_scan else 20000)
            await page.goto(url, wait_until=wait_until, timeout=goto_timeout)
            await self._poll_checkout_ready(page)
            pay_wait = 12.0 if self._gentle_scan else 28.0
            price_wait = GENTLE_PRICE_WAIT_MS if self._gentle_scan else 20000
            if self._gentle_scan or not self._fast_scan:
                await self._ensure_checkout_price_ready(
                    page,
                    region,
                    pay_wait=pay_wait,
                    price_wait_ms=price_wait,
                )

            final_url = page.url
            if final_url.startswith("chrome-error://"):
                return ScanResult(
                    code=code,
                    valid=False,
                    url=final_url,
                    status="rate_limited",
                    message="Temporary network error — will recheck automatically",
                )

            extracted = await self._run_extract_with_price_wait(page, code=code, url=url, phase="scan")
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

            if extracted.get("kind") == "prerequisite":
                name = extracted.get("name")
                msg = extracted.get("message") or "Prerequisite required to purchase this product"
                image_url = extracted.get("imageUrl")
                image_path = None
                if image_url and not self._fast_scan:
                    image_path = await self._download_image(page, image_url, code)
                raw_notes = f"{PREREQUISITE_MARKER}\n{msg}"
                game = detect_game(name, page_text, genre=extracted.get("genre")) if name else None
                return ScanResult(
                    code=code,
                    valid=True,
                    name=name,
                    price=None,
                    image_url=image_url,
                    image_path=image_path,
                    url=final_url,
                    status="valid",
                    message=msg,
                    game=game,
                    raw_notes=raw_notes,
                )

            if extracted.get("kind") == "already_owned":
                name = extracted.get("name")
                msg = extracted.get("message") or ALREADY_OWNED_MARKER
                image_url = extracted.get("imageUrl")
                image_path = None
                if image_url and not self._fast_scan:
                    image_path = await self._download_image(page, image_url, code)
                game = detect_game(name, page_text, genre=extracted.get("genre")) if name else None
                return _owned_scan_result(
                    code,
                    name=name,
                    message=msg,
                    image_url=image_url,
                    image_path=image_path,
                    url=final_url,
                    game=game,
                )

            if extracted.get("kind") == "not_eligible":
                name = extracted.get("name")
                msg = extracted.get("message") or NOT_ELIGIBLE_MARKER
                image_url = extracted.get("imageUrl")
                image_path = None
                if image_url and not self._fast_scan:
                    image_path = await self._download_image(page, image_url, code)
                game = detect_game(name, page_text, genre=extracted.get("genre")) if name else None
                return _not_eligible_scan_result(
                    code,
                    name=name,
                    message=msg,
                    image_url=image_url,
                    image_path=image_path,
                    url=final_url,
                    game=game,
                )

            kind = extracted.get("kind")
            name = extracted.get("name")
            if kind == "invalid" and name and (
                "/checkout/preload/" in final_url or "/checkout/pay/" in final_url
            ):
                kind = "valid"
            valid = kind in ("valid", "partial")
            status = kind if kind in ("valid", "partial", "invalid", "empty", "rate_limited") else "invalid"
            price = _sanitize_price(extracted.get("price"))
            image_url = extracted.get("imageUrl")
            image_path = None
            if valid and image_url and not _local_image_ready(image_path):
                image_path = await self._download_image(page, image_url, code)

            page_text = extracted.get("pageText") or ""
            requirements_text = extracted.get("requirementsText") or ""
            genre = extracted.get("genre")
            notes_parts: list[str] = []
            summary = extract_checkout_summary(page_text)
            if summary:
                notes_parts.append(f"Summary: {summary}")
            if requirements_text:
                notes_parts.append(requirements_text)
            if genre:
                notes_parts.append(f"Genre: {genre}")
            raw_notes = "\n".join(notes_parts) or None
            if valid and not image_url and not image_path:
                raw_notes = (
                    f"{raw_notes}\n{IMAGE_NONE_MARKER}".strip() if raw_notes else IMAGE_NONE_MARKER
                )
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
            entry = scan_debug.log_scan_error(code, url, detail, status="error")
            await self._emit("scan_debug", entry)
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
        await self._stop_enrich_workers(wait=False)
        async with self._activity_lock:
            self._active_scans.clear()

    async def scan_code(self, code: int, region: str = "us", headless: bool = True) -> ScanResult:
        if self._gentle_scan:
            timeout = GENTLE_SCAN_TIMEOUT_SEC
        elif self._fast_scan:
            timeout = FAST_SCAN_TIMEOUT_SEC
        else:
            timeout = SCAN_TIMEOUT_SEC
        try:
            return await asyncio.wait_for(
                self._scan_code_impl(code, region, headless),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            async with self._activity_lock:
                self._active_scans.pop(code, None)
                self._active_scan_phases.pop(code, None)
                await self._emit(
                    "scan_finished",
                    {"code": code, **self.activity_snapshot()},
                )
            return ScanResult(
                code=code,
                valid=False,
                url=self.checkout_url(code, region),
                status="timeout",
                message=f"Scan timed out after {timeout}s — likely rate limited or page stuck loading",
            )

    async def _gentle_enrich_existing(
        self, code: int, region: str, headless: bool
    ) -> ScanResult | None:
        row = await asyncio.to_thread(get_product, code)
        if not row or not row.get("valid"):
            return await self._run_gentle_scan_attempts(code, region, headless)
        base = ScanResult(
            code=code,
            valid=True,
            name=row.get("name"),
            price=row.get("price"),
            image_url=row.get("image_url"),
            image_path=row.get("image_path"),
            url=row.get("url"),
            status=row.get("status") or "valid",
            message=row.get("message"),
            game=row.get("game"),
            raw_notes=row.get("raw_notes"),
        )
        if not self._needs_browser_enrich(base):
            return base
        if self._gentle_scan:
            return await self._browser_enrich_valid(code, region, headless, base) or base
        return await self._ensure_enriched_result(code, region, headless, base)

    async def _run_gentle_scan_attempts(
        self, code: int, region: str, headless: bool
    ) -> ScanResult | None:
        # Fix mode: real browser only (HTTP often hangs; manual checkout works in Chrome).
        last = await self._browser_scan(code, region, headless)
        if last and last.valid and self._needs_browser_enrich(last):
            return await self._browser_enrich_valid(code, region, headless, last) or last
        return last

    async def _run_scan_attempts(self, code: int, region: str, headless: bool) -> ScanResult | None:
        if self._gentle_scan:
            if code in self._enrich_only_codes:
                return await self._gentle_enrich_existing(code, region, headless)
            return await self._run_gentle_scan_attempts(code, region, headless)
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
                    return await self._ensure_enriched_result(code, region, headless, last)
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
            if last and last.valid:
                last = await self._ensure_enriched_result(code, region, headless, last)
            if last is None or not _should_retry_scan(last) or attempt == MAX_SCAN_RETRIES - 1:
                break
            await asyncio.sleep(0.4 * (attempt + 1))
        return last

    async def _scan_code_body(self, code: int, region: str, headless: bool) -> ScanResult:
        if not self.session_started_at:
            self.touch_session()

        self._active_region = region

        started_at = datetime.now(timezone.utc).isoformat()
        started_mono = time.monotonic()
        async with self._activity_lock:
            self._active_scans[code] = started_at
        await self._set_scan_phase(code, "http" if self._fast_scan else "browser")

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
            self._active_scan_phases.pop(code, None)
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
        gentle_browser: bool = False,
        enrich_only_codes: set[int] | None = None,
        on_enriched: Callable[[ScanResult], Any] | None = None,
    ) -> bool:
        await self._stop_running_auto()

        self._pause_event.set()
        self.status = ScanStatus.RUNNING
        self._fast_scan = not browser_only
        self._gentle_scan = gentle_browser and browser_only
        self._use_chrome_browser = self._gentle_scan
        self._enrich_only_codes = enrich_only_codes or set()
        self._last_progress_emit = 0.0
        self._rate_limit_pause_until = 0.0
        workers = max(1, concurrency)
        if self._gentle_scan:
            workers = min(workers, FIX_BROWSER_MAX_CONCURRENCY)
        elif self._fast_scan:
            workers = min(workers, AUTO_SCAN_MAX_CONCURRENCY)
        nav_cap = min(workers, BROWSER_POOL_MAX)
        self._nav_semaphore = asyncio.Semaphore(nav_cap)
        self._http_semaphore = asyncio.Semaphore(workers)
        await self._dispose_httpx()
        if self._fast_scan or self._gentle_scan:
            await self._ensure_httpx(pool_size=max(workers, HTTP_PROBE_CONCURRENCY))
        worker_delay_ms = max(delay_ms, 400) if self._gentle_scan else delay_ms
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
            {
                "session_started_at": session_started_at,
                "workers": workers,
                "mode": "gentle" if self._gentle_scan else "fast" if self._fast_scan else "browser",
                **self.activity_snapshot(),
            },
            wait=True,
        )

        if not codes:
            self.status = ScanStatus.IDLE
            await self._emit("auto_finished", {"message": "All codes in range already scanned"})
            return True

        if self._gentle_scan:
            await self._ensure_pool_size(nav_cap, headless)
        elif self._fast_scan and on_enriched:
            await self._start_enrich_workers(region, headless, on_enriched)

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

        async def worker(worker_id: int) -> None:
            nonlocal cursor, highest_done
            if worker_id > 0 and worker_delay_ms > 0:
                await asyncio.sleep((worker_delay_ms / 1000) * worker_id)
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

                await self._set_scan_phase(code, "queued")
                pause_until = self._rate_limit_pause_until
                if pause_until > time.monotonic():
                    await asyncio.sleep(pause_until - time.monotonic())
                result = await self.scan_code(code, region=region, headless=headless)
                await self._clear_scan_phase(code)
                if self._gentle_scan and result.status in ("rate_limited", "timeout", "throttled"):
                    self._rate_limit_pause_until = time.monotonic() + RATE_LIMIT_PAUSE_SEC

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

                if worker_delay_ms > 0 and not self._stop_requested:
                    await asyncio.sleep(worker_delay_ms / 1000)

        async def runner() -> None:
            tasks: list[asyncio.Task] = []
            try:
                tasks = [asyncio.create_task(worker(i)) for i in range(workers)]
                await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                for task in tasks:
                    task.cancel()
                raise
            finally:
                self._fast_scan = False
                self._gentle_scan = False
                self._use_chrome_browser = False
                self._enrich_only_codes = set()
                await self._dispose_httpx()
                if self._enrich_queue is not None:
                    try:
                        await asyncio.wait_for(self._enrich_queue.join(), timeout=300)
                    except asyncio.TimeoutError:
                        pass
                await self._stop_enrich_workers(wait=True)
                if self.status != ScanStatus.NEEDS_LOGIN:
                    self.status = ScanStatus.IDLE
                async with self._activity_lock:
                    self._active_scans.clear()
                    self._active_scan_phases.clear()
                await self._emit("auto_finished", {"last_code": highest_done}, wait=True)

        self._auto_task = asyncio.create_task(runner())
        return True
