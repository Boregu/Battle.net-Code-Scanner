import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.games import detect_game

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "library.db"
IMAGES_DIR = DATA_DIR / "images"
SETTINGS_PATH = DATA_DIR / "settings.json"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)


def get_connection() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            code INTEGER PRIMARY KEY,
            name TEXT,
            price TEXT,
            image_path TEXT,
            image_url TEXT,
            url TEXT,
            valid INTEGER NOT NULL DEFAULT 0,
            checked_at TEXT NOT NULL,
            status TEXT,
            message TEXT,
            raw_notes TEXT
        )
        """
    )
    _migrate_schema(conn)
    conn.commit()
    return conn


def _migrate_schema(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
    if "status" not in columns:
        conn.execute("ALTER TABLE products ADD COLUMN status TEXT")
    if "message" not in columns:
        conn.execute("ALTER TABLE products ADD COLUMN message TEXT")
    if "game" not in columns:
        conn.execute("ALTER TABLE products ADD COLUMN game TEXT")


def _normalize_price(price: str | None) -> str | None:
    if price is None:
        return None
    trimmed = str(price).strip()
    if not trimmed:
        return None
    if re.match(r"^[$€£¥₩]", trimmed):
        return trimmed
    try:
        amount = float(trimmed.replace(",", ""))
    except ValueError:
        return trimmed
    if amount == 0:
        return "$0.00"
    if amount == round(amount, 2):
        return f"${amount:.2f}"
    return f"${amount}"


def _normalize_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["valid"] = bool(data.get("valid"))
    if data.get("price") is not None:
        data["price"] = _normalize_price(data.get("price"))
    if not data.get("status"):
        data["status"] = "valid" if data["valid"] else "invalid"
    message = (data.get("message") or "").lower()
    if data.get("status") == "invalid" and ("nothing here" in message or data.get("message") == "Product not found"):
        data["status"] = "empty"
    if not data.get("game"):
        raw_notes = data.get("raw_notes") or ""
        genre = None
        for line in raw_notes.splitlines():
            if line.lower().startswith("genre:"):
                genre = line.split(":", 1)[1].strip()
                break
        data["game"] = detect_game(data.get("name"), raw_notes, genre=genre)
    return data


def upsert_product(entry: dict[str, Any], *, force: bool = False, merge_missing: bool = False) -> dict[str, Any]:
    conn = get_connection()
    row = conn.execute("SELECT * FROM products WHERE code = ?", (entry["code"],)).fetchone()
    existing = dict(row) if row else None
    if existing:
        existing["valid"] = bool(existing.get("valid"))
    if existing and existing.get("valid") and not entry.get("valid") and not force:
        conn.close()
        return _normalize_row(row)

    merged = dict(entry)
    if existing and (not force or merge_missing):
        for field in ("name", "price", "image_path", "image_url", "url", "game", "raw_notes"):
            if not merged.get(field) and existing.get(field):
                merged[field] = existing[field]
        if existing.get("name") and not merged.get("name"):
            merged["status"] = existing.get("status") or merged.get("status")
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO products (code, name, price, image_path, image_url, url, valid, checked_at, status, message, game, raw_notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
            name=excluded.name,
            price=excluded.price,
            image_path=excluded.image_path,
            image_url=excluded.image_url,
            url=excluded.url,
            valid=excluded.valid,
            checked_at=excluded.checked_at,
            status=excluded.status,
            message=excluded.message,
            game=excluded.game,
            raw_notes=excluded.raw_notes
        """,
        (
            merged["code"],
            merged.get("name"),
            merged.get("price"),
            merged.get("image_path"),
            merged.get("image_url"),
            merged.get("url"),
            1 if merged.get("valid") else 0,
            merged.get("checked_at", now),
            merged.get("status"),
            merged.get("message"),
            merged.get("game"),
            merged.get("raw_notes"),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM products WHERE code = ?", (merged["code"],)).fetchone()
    conn.close()
    return _normalize_row(row)


def _library_filters(
    *,
    valid_only: bool = False,
    search: str = "",
    game: str = "",
) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    if valid_only:
        clauses.append("valid = 1")
    if search.strip():
        clauses.append(
            "(CAST(code AS TEXT) LIKE ? OR IFNULL(name, '') LIKE ? OR IFNULL(game, '') LIKE ?)"
        )
        like = f"%{search.strip()}%"
        params.extend([like, like, like])
    if game.strip():
        if game.strip().lower() in ("unknown", "unknown game"):
            clauses.append("(IFNULL(game, '') = '' OR IFNULL(game, '') = 'Unknown game')")
        else:
            clauses.append("IFNULL(game, '') = ?")
            params.append(game.strip())
    return clauses, params


def count_products(valid_only: bool = False, search: str = "", game: str = "") -> int:
    conn = get_connection()
    query = "SELECT COUNT(*) FROM products"
    clauses, params = _library_filters(valid_only=valid_only, search=search, game=game)

    if clauses:
        query += " WHERE " + " AND ".join(clauses)

    row = conn.execute(query, params).fetchone()
    conn.close()
    return int(row[0])


SORT_OPTIONS: dict[str, str] = {
    "code": "code ASC",
    "code_desc": "code DESC",
    "game": "IFNULL(game, 'zzz') ASC, code ASC",
    "game_desc": "IFNULL(game, '') DESC, code ASC",
    "name": "IFNULL(name, 'zzz') ASC, code ASC",
    "checked": "checked_at DESC",
}


def list_games(*, valid_only: bool = True) -> list[dict[str, Any]]:
    conn = get_connection()
    query = """
        SELECT IFNULL(NULLIF(game, ''), 'Unknown game') AS game, COUNT(*) AS count
        FROM products
    """
    params: list[Any] = []
    if valid_only:
        query += " WHERE valid = 1"
    query += " GROUP BY IFNULL(NULLIF(game, ''), 'Unknown game') ORDER BY count DESC, game ASC"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [{"game": row[0], "count": int(row[1])} for row in rows]


def list_products(
    valid_only: bool = False,
    search: str = "",
    game: str = "",
    sort: str = "code",
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    conn = get_connection()
    query = "SELECT * FROM products"
    clauses, params = _library_filters(valid_only=valid_only, search=search, game=game)

    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    order = SORT_OPTIONS.get(sort, SORT_OPTIONS["code"])
    query += f" ORDER BY {order}"
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        params.extend([max(1, limit), max(0, offset)])

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [_normalize_row(row) for row in rows]


def get_product(code: int) -> dict[str, Any] | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM products WHERE code = ?", (code,)).fetchone()
    conn.close()
    return _normalize_row(row) if row else None


def get_scanned_codes() -> set[int]:
    conn = get_connection()
    rows = conn.execute("SELECT code FROM products").fetchall()
    conn.close()
    return {row[0] for row in rows}


def get_scanned_codes_in_range(start: int, end: int) -> set[int]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT code FROM products WHERE code >= ? AND code <= ?",
        (start, end),
    ).fetchall()
    conn.close()
    return {row[0] for row in rows}


def get_valid_codes_in_range(start: int, end: int) -> set[int]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT code FROM products WHERE code >= ? AND code <= ? AND valid = 1",
        (start, end),
    ).fetchall()
    conn.close()
    return {row[0] for row in rows}


def get_confirmed_empty_codes_in_range(start: int, end: int) -> set[int]:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT code FROM products
        WHERE code >= ? AND code <= ? AND valid = 0
          AND (
            status = 'empty'
            OR (
              status = 'invalid'
              AND (
                LOWER(message) LIKE '%nothing here%'
                OR message = 'Product not found'
              )
            )
          )
        """,
        (start, end),
    ).fetchall()
    conn.close()
    return {row[0] for row in rows}


def get_skip_codes_in_range(
    start: int,
    end: int,
    *,
    skip_valid: bool,
    skip_no_product: bool,
) -> set[int]:
    codes: set[int] = set()
    if skip_valid:
        codes |= get_valid_codes_in_range(start, end)
    if skip_no_product:
        codes |= get_confirmed_empty_codes_in_range(start, end)
    return codes


def get_invalid_codes_in_range(start: int, end: int) -> list[int]:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT code FROM products
        WHERE code >= ? AND code <= ? AND valid = 0
          AND status IN ('empty', 'invalid', 'error', 'rate_limited', 'timeout', 'server_error', 'needs_login')
        ORDER BY code
        """,
        (start, end),
    ).fetchall()
    conn.close()
    return [row[0] for row in rows]


def count_incomplete_valid_codes() -> int:
    conn = get_connection()
    row = conn.execute(
        """
        SELECT COUNT(*) FROM products
        WHERE valid = 1
          AND (
            IFNULL(name, '') = ''
            OR IFNULL(price, '') = ''
            OR IFNULL(image_path, '') = ''
            OR IFNULL(game, '') = ''
          )
        """
    ).fetchone()
    conn.close()
    return int(row[0])


def get_failed_codes_in_range(start: int, end: int) -> list[int]:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT code FROM products
        WHERE code >= ? AND code <= ? AND valid = 0
          AND status IN ('invalid', 'error', 'rate_limited', 'timeout', 'server_error', 'needs_login')
        ORDER BY code
        """,
        (start, end),
    ).fetchall()
    conn.close()
    return [row[0] for row in rows]


def get_incomplete_valid_codes() -> list[int]:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT code FROM products
        WHERE valid = 1
          AND (
            IFNULL(name, '') = ''
            OR IFNULL(price, '') = ''
            OR IFNULL(image_path, '') = ''
            OR IFNULL(game, '') = ''
          )
        ORDER BY code
        """
    ).fetchall()
    conn.close()
    return [row[0] for row in rows]


def next_unscanned_code(
    start: int,
    end: int,
    *,
    skip_valid: bool = True,
    skip_no_product: bool = False,
) -> int | None:
    if start > end:
        return None
    skip_codes = get_skip_codes_in_range(
        start,
        end,
        skip_valid=skip_valid,
        skip_no_product=skip_no_product,
    )
    for code in range(start, end + 1):
        if code not in skip_codes:
            return code
    return None


def list_recent_products(limit: int = 5) -> list[dict[str, Any]]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM products ORDER BY checked_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [_normalize_row(row) for row in rows]


def delete_product(code: int) -> bool:
    conn = get_connection()
    cur = conn.execute("DELETE FROM products WHERE code = ?", (code,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    if deleted:
        image_path = IMAGES_DIR / f"{code}.jpg"
        if image_path.exists():
            image_path.unlink()
        for ext in (".png", ".webp", ".jpeg"):
            alt = IMAGES_DIR / f"{code}{ext}"
            if alt.exists():
                alt.unlink()
    return deleted


def load_settings() -> dict[str, Any]:
    ensure_dirs()
    default = {
        "region": "us",
        "start_code": 64300,
        "end_code": 64400,
        "current_code": 64300,
        "delay_ms": 2000,
        "concurrency": 2,
        "skip_scanned": True,
        "skip_no_product": False,
        "headless": True,
    }
    if not SETTINGS_PATH.exists():
        with SETTINGS_PATH.open("w", encoding="utf-8") as f:
            json.dump(default, f, indent=2)
        return default.copy()
    with SETTINGS_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {**default, **data}


def save_settings(settings: dict[str, Any]) -> dict[str, Any]:
    ensure_dirs()
    current = load_settings()
    current.update(settings)
    with SETTINGS_PATH.open("w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)
    return current


def export_library() -> list[dict[str, Any]]:
    return list_products()
