import json
import shutil
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "data" / "scanner-debug.log"
MAX_MEMORY = 250
MAX_FILE_BYTES = 900_000

_buffer: deque[dict[str, Any]] = deque(maxlen=MAX_MEMORY)
_lock = threading.Lock()


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _trim_file() -> None:
    if not LOG_PATH.exists():
        return
    try:
        if LOG_PATH.stat().st_size <= MAX_FILE_BYTES:
            return
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        LOG_PATH.write_text("\n".join(lines[-400:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def log(level: str, event: str, **fields: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"ts": _ts(), "level": level, "event": event, **fields}
    line = json.dumps(entry, ensure_ascii=False, default=str)
    with _lock:
        _buffer.append(entry)
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            with LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
            _trim_file()
        except Exception:
            pass
    if level in ("warn", "error", "critical"):
        print(f"[scanner-debug] {line}", file=sys.stderr, flush=True)
    return entry


def recent(limit: int = 100) -> list[dict[str, Any]]:
    cap = max(1, min(limit, MAX_MEMORY))
    with _lock:
        return list(_buffer)[-cap:]


def entries_for_code(code: int, limit: int = 8) -> list[dict[str, Any]]:
    cap = max(1, min(limit, 20))
    with _lock:
        matches = [entry for entry in _buffer if entry.get("code") == code]
    if len(matches) >= cap:
        return matches[-cap:]
    seen = {entry.get("ts") for entry in matches}
    if LOG_PATH.exists():
        try:
            for line in reversed(LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()):
                if len(matches) >= cap:
                    break
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("code") != code or entry.get("ts") in seen:
                    continue
                matches.insert(0, entry)
                seen.add(entry.get("ts"))
        except Exception:
            pass
    return matches[-cap:]


def _syntax_hint(detail: str) -> str | None:
    lower = detail.lower()
    if "syntaxerror" not in lower and "unexpected identifier" not in lower:
        return None
    return (
        "EXTRACT_SCRIPT has a JavaScript syntax error in app/scanner.py "
        "(e.g. missing parentheses after if/for)"
    )


def log_extract_error(
    code: int | None,
    phase: str,
    url: str,
    detail: str,
) -> dict[str, Any]:
    return log(
        "error",
        "extract_failed",
        code=code,
        phase=phase,
        url=url,
        error=detail,
        hint=_syntax_hint(detail),
    )


def log_extract_result(
    code: int | None,
    phase: str,
    url: str,
    extracted: dict[str, Any],
) -> None:
    kind = extracted.get("kind")
    name = extracted.get("name")
    price = extracted.get("price")
    if kind in ("valid", "partial") and not price and "/checkout/pay/" in (url or ""):
        log("warn", "missing_price", code=code, phase=phase, url=url, name=name, kind=kind)
    elif kind == "already_owned":
        log("info", "already_owned", code=code, phase=phase, url=url, name=extracted.get("name"))
    elif kind == "not_eligible":
        log("info", "not_eligible", code=code, phase=phase, url=url, name=extracted.get("name"))
    elif kind not in ("valid", "partial", "empty", "login", "rate_limited", "prerequisite", "already_owned", "not_eligible") and name:
        log(
            "warn",
            "weak_extract",
            code=code,
            phase=phase,
            url=url,
            kind=kind,
            name=name,
            price=price,
            message=extracted.get("message"),
        )


def log_scan_error(code: int, url: str, detail: str, *, status: str = "error") -> dict[str, Any]:
    return log(
        "error",
        "scan_failed",
        code=code,
        url=url,
        status=status,
        error=detail,
        hint=_syntax_hint(detail),
    )


def validate_js_with_node(script_body: str) -> tuple[bool | None, str | None]:
    node = shutil.which("node")
    if not node:
        return None, None
    check = (
        "try { const fn = "
        + script_body.strip()
        + "; if (typeof fn !== 'function') throw new Error('EXTRACT_SCRIPT is not a function'); "
        + "} catch (e) { console.error(e.stack || e.message); process.exit(1); }"
    )
    try:
        proc = subprocess.run(
            [node, "-e", check],
            capture_output=True,
            text=True,
            timeout=8,
            cwd=ROOT,
        )
    except Exception:
        return None, None
    if proc.returncode == 0:
        return True, None
    err = (proc.stderr or proc.stdout or "unknown error").strip()[:800]
    return False, err
