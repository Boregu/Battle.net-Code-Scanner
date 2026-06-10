import json
import re
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from app.catalog_tags import (
    apply_auto_catalog_tags,
    get_product_includes,
    needs_tag_review,
    parse_catalog_tags,
    preserve_catalog_tags,
    resolve_catalog_tags,
    tag_labels,
    tag_options_for_game,
    write_catalog_tags,
)
from app.games import detect_game

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATABASES_DIR = DATA_DIR / "databases"
LEGACY_DB_PATH = DATA_DIR / "library.db"
IMAGES_DIR = DATA_DIR / "images"
SETTINGS_PATH = DATA_DIR / "settings.json"
_DB_NAME_RE = re.compile(r"[^\w\-]+")


def _read_settings_file() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    with SETTINGS_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def sanitize_database_name(name: str) -> str:
    cleaned = _DB_NAME_RE.sub("-", (name or "").strip()).strip("-").lower()
    return cleaned or "library"


def active_database_name() -> str:
    raw = _read_settings_file().get("active_database") or "library"
    return sanitize_database_name(str(raw))


def get_db_path(name: str | None = None) -> Path:
    db_name = sanitize_database_name(name) if name else active_database_name()
    return DATABASES_DIR / f"{db_name}.db"


def migrate_legacy_layout() -> None:
    DATABASES_DIR.mkdir(parents=True, exist_ok=True)
    active_path = get_db_path("library")
    if LEGACY_DB_PATH.exists() and not active_path.exists():
        shutil.copy2(LEGACY_DB_PATH, active_path)
    active = active_database_name()
    if active != "library" and not get_db_path(active).exists() and LEGACY_DB_PATH.exists():
        shutil.copy2(LEGACY_DB_PATH, get_db_path(active))


def _database_stats(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "exists": False,
            "rows": 0,
            "valid_count": 0,
            "size_bytes": 0,
            "modified_at": None,
        }
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT COUNT(*), COALESCE(SUM(valid), 0) FROM products").fetchone()
        rows = int(row[0] or 0)
        valid_count = int(row[1] or 0)
    except sqlite3.Error:
        rows = 0
        valid_count = 0
    finally:
        conn.close()
    stat = path.stat()
    return {
        "exists": True,
        "rows": rows,
        "valid_count": valid_count,
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }


def database_info(name: str) -> dict[str, Any]:
    clean = sanitize_database_name(name)
    path = get_db_path(clean)
    stats = _database_stats(path)
    return {
        "name": clean,
        "path": str(path),
        "active": clean == active_database_name(),
        **stats,
    }


def list_databases() -> list[dict[str, Any]]:
    migrate_legacy_layout()
    active = active_database_name()
    names = sorted({path.stem for path in DATABASES_DIR.glob("*.db")})
    if "library" not in names and get_db_path("library").exists():
        names.append("library")
    names = sorted(set(names))
    return [database_info(name) for name in names if get_db_path(name).exists()]


def list_backup_snapshots() -> list[dict[str, Any]]:
    backups_dir = DATA_DIR / "backups"
    if not backups_dir.exists():
        return []
    snapshots: list[dict[str, Any]] = []
    for path in sorted(backups_dir.iterdir(), reverse=True):
        if not path.is_dir():
            continue
        db_file = path / "library.db"
        if not db_file.exists():
            continue
        stats = _database_stats(db_file)
        snapshots.append(
            {
                "id": path.name,
                "path": str(path),
                "rows": stats["rows"],
                "valid_count": stats["valid_count"],
                "size_bytes": stats["size_bytes"],
                "has_settings": (path / "settings.json").exists(),
            }
        )
    return snapshots


def create_database(name: str, *, switch: bool = False) -> dict[str, Any]:
    ensure_dirs()
    clean = sanitize_database_name(name)
    path = get_db_path(clean)
    if path.exists():
        raise ValueError(f"Database '{clean}' already exists")
    conn = sqlite3.connect(path)
    try:
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
    finally:
        conn.close()
    if switch:
        return switch_database(clean)
    return database_info(clean)


def switch_database(name: str) -> dict[str, Any]:
    clean = sanitize_database_name(name)
    path = get_db_path(clean)
    if not path.exists():
        raise FileNotFoundError(f"Database '{clean}' not found")
    settings = load_settings()
    settings["active_database"] = clean
    save_settings(settings)
    invalidate_stats_cache()
    return database_info(clean)


def restore_database_from_backup(
    backup_id: str,
    *,
    target_name: str | None = None,
    switch: bool = True,
    restore_settings: bool = True,
) -> dict[str, Any]:
    ensure_dirs()
    backup_dir = DATA_DIR / "backups" / backup_id
    source = backup_dir / "library.db"
    if not source.exists():
        raise FileNotFoundError(f"Backup '{backup_id}' not found or has no library.db")
    clean = sanitize_database_name(target_name or f"restored-{backup_id[:8]}")
    dest = get_db_path(clean)
    shutil.copy2(source, dest)
    conn = sqlite3.connect(dest)
    try:
        _migrate_schema(conn)
        conn.commit()
    finally:
        conn.close()
    if switch:
        switch_database(clean)
    if restore_settings:
        settings_path = backup_dir / "settings.json"
        if settings_path.exists():
            backup_settings = json.loads(settings_path.read_text(encoding="utf-8"))
            current = load_settings()
            for key in (
                "region",
                "start_code",
                "end_code",
                "current_code",
                "delay_ms",
                "concurrency",
                "skip_scanned",
                "skip_no_product",
                "headless",
            ):
                if key in backup_settings:
                    current[key] = backup_settings[key]
            current["active_database"] = clean
            save_settings(current)
    publish_catalog_snapshots()
    return {
        "database": database_info(clean),
        "backup_id": backup_id,
        "restored_settings": restore_settings and (backup_dir / "settings.json").exists(),
    }


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    migrate_legacy_layout()


def get_connection() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(get_db_path(), check_same_thread=False)
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_valid_game ON products(valid, game) WHERE valid = 1"
    )


def _normalize_price(price: str | None) -> str | None:
    if price is None:
        return None
    trimmed = str(price).strip()
    if not trimmed:
        return None
    if not is_usable_price(trimmed):
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


def is_usable_price(price: str | None) -> bool:
    if price is None:
        return False
    trimmed = str(price).strip()
    if not trimmed:
        return False
    if re.match(r"^0[\s,.-]*((overwatch\s*)?coins\b|$)", trimmed, re.I):
        return False
    coin_match = re.match(
        r"^([\d,]+)\s+(Overwatch[\u00ae\u2122\s]*Coins|Coins)\b",
        trimmed,
        re.I,
    )
    if coin_match:
        try:
            return int(coin_match.group(1).replace(",", "")) > 0
        except ValueError:
            return False
    currency_line = re.match(
        r"^(EUR|USD|GBP|CHF|SEK|NOK|DKK|PLN|CZK|TWD|KRW)\s*-\s*([\d][\d.,]*)",
        trimmed,
        re.I,
    )
    if currency_line:
        try:
            return float(currency_line.group(2).replace(",", "")) >= 0
        except ValueError:
            return False
    money_match = re.match(r"^[$€£¥₩]\s*([\d][\d.,]*)", trimmed)
    if money_match:
        try:
            return float(money_match.group(1).replace(",", "")) >= 0
        except ValueError:
            return False
    cp_match = re.match(r"^([\d,]+)\s*(?:\(\+\s*[\d,]+\s*Bonus\s*\))?\s*CP\b", trimmed, re.I)
    if cp_match:
        try:
            return int(cp_match.group(1).replace(",", "")) > 0
        except ValueError:
            return False
    if re.match(r"^[\d][\d.,]*\s*\(was\s+", trimmed, re.I):
        try:
            return float(re.match(r"^([\d.,]+)", trimmed).group(1).replace(",", "")) > 0
        except (ValueError, AttributeError):
            return False
    return False


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
    if not data.get("game") and data.get("name"):
        genre_match = re.search(r"Genre:\s*(.+)", data.get("raw_notes") or "", re.I)
        genre = genre_match.group(1).strip() if genre_match else None
        data["game"] = detect_game(data.get("name"), data.get("raw_notes") or "", genre=genre)
    elif data.get("name"):
        genre_match = re.search(r"Genre:\s*(.+)", data.get("raw_notes") or "", re.I)
        genre = genre_match.group(1).strip() if genre_match else None
        from_name = detect_game(data.get("name"), "", genre=genre)
        if from_name:
            data["game"] = from_name
    if data.get("valid"):
        tags = resolve_catalog_tags(data)
        data["catalog_tags"] = tags
        data["catalog_tag_labels"] = tag_labels(tags)
        data["includes"] = get_product_includes(
            data.get("name") or "",
            data.get("game"),
            tags,
        )
    else:
        data["catalog_tags"] = []
        data["catalog_tag_labels"] = []
        data["includes"] = None
    return data


_MERGE_FIELDS = ("name", "price", "image_path", "image_url", "url", "game", "raw_notes")


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _merge_existing_fields(existing: dict[str, Any], merged: dict[str, Any]) -> dict[str, Any]:
    out = dict(merged)
    for field in _MERGE_FIELDS:
        if not _has_value(out.get(field)) and _has_value(existing.get(field)):
            out[field] = existing[field]
    if _has_value(out.get("raw_notes")) and _has_value(existing.get("raw_notes")):
        out["raw_notes"] = preserve_catalog_tags(existing.get("raw_notes"), out.get("raw_notes"))
    elif not _has_value(out.get("raw_notes")) and _has_value(existing.get("raw_notes")):
        out["raw_notes"] = existing.get("raw_notes")
    if existing.get("valid") and out.get("valid"):
        if _has_value(existing.get("name")) and _has_value(existing.get("price")):
            out["status"] = existing.get("status") or "valid"
            if not _has_value(out.get("message")):
                out["message"] = existing.get("message")
    elif _has_value(existing.get("name")) and not _has_value(out.get("name")):
        out["status"] = existing.get("status") or out.get("status")
    return out


def _apply_scan_failure_tracking(
    existing: dict[str, Any] | None, merged: dict[str, Any]
) -> dict[str, Any]:
    status = (merged.get("status") or "").strip()
    if merged.get("valid") and status in ("valid", "partial", "not_eligible", "owned"):
        notes = (merged.get("raw_notes") or "").strip()
        notes = SCAN_FAILURES_RE.sub("", notes).strip()
        notes = notes.replace(THROTTLED_MARKER, "").strip()
        merged["raw_notes"] = notes or None
        return merged
    if status not in ("timeout", "rate_limited", "error", "server_error"):
        return merged

    prev_notes = (existing or {}).get("raw_notes") or ""
    match = SCAN_FAILURES_RE.search(prev_notes)
    failures = int(match.group(1)) if match else 0
    failures += 1
    notes = SCAN_FAILURES_RE.sub("", prev_notes).strip()
    notes = f"scan_failures: {failures}" + (f"\n{notes}" if notes else "")
    merged["raw_notes"] = notes
    if failures >= MAX_AUTO_RETRIES and status in ("timeout", "rate_limited"):
        merged["status"] = "throttled"
        merged["message"] = (
            f"{THROTTLED_MARKER} — stop retrying for now; wait an hour or scan one manually later"
        )
        if THROTTLED_MARKER not in notes:
            merged["raw_notes"] = f"{THROTTLED_MARKER}\n{notes}"
    return merged


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
    should_merge = existing and (
        not force
        or merge_missing
        or (existing.get("valid") and entry.get("valid"))
    )
    if should_merge:
        merged = _merge_existing_fields(existing, merged)
    merged = apply_prerequisite_notes(merged)
    merged = apply_already_owned_notes(merged)
    merged = apply_not_eligible_notes(merged)
    merged = _apply_scan_failure_tracking(existing, merged)
    merged = apply_auto_catalog_tags(merged)
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
    invalidate_stats_cache()
    return _normalize_row(row)


def _throttled_sql(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"""(
            {prefix}status = 'throttled'
            OR LOWER(IFNULL({prefix}raw_notes, '')) LIKE '%retry later — battle.net is throttling%'
            OR CAST(IFNULL({prefix}raw_notes, '') AS TEXT) GLOB '*scan_failures: [2-9]*'
          )"""


def _not_eligible_sql(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"""(
            {prefix}status = 'not_eligible'
            OR LOWER(IFNULL({prefix}raw_notes, '')) LIKE '%scan account not eligible%'
            OR LOWER(IFNULL({prefix}message, '')) LIKE '%not eligible to purchase%'
            OR LOWER(IFNULL({prefix}message, '')) LIKE '%not eligible for this product%'
          )"""


def _already_owned_sql(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"""(
            LOWER(IFNULL({prefix}raw_notes, '')) LIKE '%scan account already owns%'
            OR LOWER(IFNULL({prefix}message, '')) LIKE '%already owns this product%'
            OR LOWER(IFNULL({prefix}message, '')) LIKE '%already have access to this product%'
          )"""


def _account_gated_sql(alias: str = "") -> str:
    return f"({_not_eligible_sql(alias)} OR {_already_owned_sql(alias)})"


def _library_filters(
    *,
    valid_only: bool = False,
    failed_only: bool = False,
    incomplete_only: bool = False,
    not_eligible_only: bool = False,
    search: str = "",
    game: str = "",
) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    filter_parts: list[str] = []
    if valid_only:
        filter_parts.append("valid = 1")
    if failed_only:
        filter_parts.append(_failed_sql())
    if incomplete_only:
        filter_parts.append(_incomplete_valid_sql())
    if not_eligible_only:
        filter_parts.append(_not_eligible_sql())
    if filter_parts:
        if len(filter_parts) == 1:
            clauses.append(filter_parts[0])
        else:
            wrapped = " OR ".join(f"({part})" for part in filter_parts)
            clauses.append(f"({wrapped})")
        if failed_only or incomplete_only or not_eligible_only:
            clauses.append(f"NOT ({_confirmed_empty_sql()})")
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


def count_products(
    valid_only: bool = False,
    failed_only: bool = False,
    incomplete_only: bool = False,
    not_eligible_only: bool = False,
    search: str = "",
    game: str = "",
) -> int:
    conn = get_connection()
    query = "SELECT COUNT(*) FROM products"
    clauses, params = _library_filters(
        valid_only=valid_only,
        failed_only=failed_only,
        incomplete_only=incomplete_only,
        not_eligible_only=not_eligible_only,
        search=search,
        game=game,
    )

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
    failed_only: bool = False,
    incomplete_only: bool = False,
    not_eligible_only: bool = False,
    search: str = "",
    game: str = "",
    sort: str = "code",
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    conn = get_connection()
    query = "SELECT * FROM products"
    clauses, params = _library_filters(
        valid_only=valid_only,
        failed_only=failed_only,
        incomplete_only=incomplete_only,
        not_eligible_only=not_eligible_only,
        search=search,
        game=game,
    )

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


def set_catalog_tags(code: int, tags: list[str]) -> dict[str, Any] | None:
    conn = get_connection()
    row = conn.execute("SELECT * FROM products WHERE code = ?", (code,)).fetchone()
    if not row:
        conn.close()
        return None
    data = dict(row)
    data["raw_notes"] = write_catalog_tags(data.get("raw_notes"), tags)
    conn.execute(
        "UPDATE products SET raw_notes = ? WHERE code = ?",
        (data["raw_notes"], code),
    )
    conn.commit()
    conn.close()
    invalidate_stats_cache()
    invalidate_tag_queue_cache()
    return _normalize_row(data)


_tag_queue_count_cache: dict[str, tuple[int, float]] = {}
TAG_QUEUE_COUNT_TTL = 30.0


def invalidate_tag_queue_cache() -> None:
    _tag_queue_count_cache.clear()


def count_tag_queue(*, game: str = "", use_cache: bool = True) -> int:
    return len(_tag_queue_rows(game=game))


def _tag_queue_rows(*, game: str = "") -> list[dict[str, Any]]:
    conn = get_connection()
    clauses = ["valid = 1", "game IN ('Overwatch', 'StarCraft II')"]
    params: list[Any] = []
    if game:
        clauses.append("game = ?")
        params.append(game)
    query = f"""
        SELECT * FROM products
        WHERE {' AND '.join(clauses)}
        ORDER BY code ASC
    """
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [_normalize_row(row) for row in rows if needs_tag_review(dict(row))]


def get_tag_queue_codes(
    *,
    game: str = "",
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    rows = _tag_queue_rows(game=game)
    start = max(0, offset)
    end = start + max(1, limit)
    return rows[start:end]


def backfill_auto_catalog_tags(*, game: str = "") -> int:
    conn = get_connection()
    clauses = ["valid = 1", "game IN ('Overwatch', 'StarCraft II')"]
    params: list[Any] = []
    if game:
        clauses.append("game = ?")
        params.append(game)
    clauses.append("(raw_notes IS NULL OR raw_notes NOT LIKE '%catalog_tags:%')")
    rows = conn.execute(
        f"SELECT * FROM products WHERE {' AND '.join(clauses)} ORDER BY code ASC",
        params,
    ).fetchall()
    updated = 0
    for row in rows:
        data = dict(row)
        tagged = apply_auto_catalog_tags(data)
        new_notes = tagged.get("raw_notes")
        if new_notes and new_notes != data.get("raw_notes"):
            conn.execute(
                "UPDATE products SET raw_notes = ? WHERE code = ?",
                (new_notes, data["code"]),
            )
            updated += 1
    conn.commit()
    conn.close()
    invalidate_stats_cache()
    invalidate_tag_queue_cache()
    return updated


def explain_product(row: dict[str, Any], *, log_entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    notes = (row.get("raw_notes") or "").strip()
    notes_lower = notes.lower()
    msg = (row.get("message") or "").strip()
    msg_lower = msg.lower()
    status = (row.get("status") or "").strip()
    valid = bool(row.get("valid"))
    has_price = bool((row.get("price") or "").strip())
    has_image = bool((row.get("image_path") or "").strip() or (row.get("image_url") or "").strip())
    image_none = "image: none" in notes_lower
    name = (row.get("name") or "").strip()

    reasons: list[str] = []
    fixes: list[str] = []
    category = "unknown"

    if not valid:
        category = "empty" if status == "empty" else "failed"
        if status == "empty":
            reasons.append("Battle.net returned nothing at this code.")
        elif status == "timeout":
            reasons.append("Scan timed out — usually too many parallel browser tabs or Battle.net throttling.")
            fixes.append("Scan One on this code (keep concurrency ≤8).")
        elif status == "rate_limited":
            reasons.append("Battle.net rate-limited the request.")
            fixes.append("Wait a minute, then Scan One or Fix incomplete.")
        elif status == "needs_login":
            reasons.append("Not logged in — checkout needs a saved Battle.net session.")
            fixes.append("Open login, sign in, Save session.")
        elif msg:
            reasons.append(msg)
            fixes.append("Scan One or Fix problems.")
        else:
            reasons.append(f"Scan failed ({status or 'unknown'}).")
            fixes.append("Scan One on this code.")
    elif "prerequisite required" in notes_lower or "you need" in msg_lower and "purchase" in msg_lower:
        category = "prerequisite"
        reasons.append("This is an expansion/add-on. Battle.net won't show a price unless this account owns the base game.")
        if not has_image:
            reasons.append("Image wasn't saved — browser enrich likely didn't finish.")
            fixes.append("Scan One still helps for the cover art.")
        if not has_price:
            reasons.append("No price is expected here unless you own the prerequisite on this account.")
    elif "scan account already owns" in notes_lower or "already have access" in msg_lower:
        category = "owned"
        reasons.append("This Battle.net account already owns the product — no checkout price.")
        if not has_image:
            reasons.append("Image missing from the library row.")
            fixes.append("Scan One to refresh image.")
    elif status == "throttled" or (
        "retry later" in notes_lower and "throttling" in notes_lower
    ):
        category = "throttled"
        reasons.append("Battle.net kept timing out or rate-limiting this code.")
        reasons.append("Tagged to stop auto-retry — wait an hour, then Scan One manually if you want.")
    elif "scan account not eligible" in notes_lower or "not eligible" in msg_lower or status == "not_eligible":
        category = "not_eligible"
        reasons.append("This account can't buy this product — another Battle.net account would be needed.")
        reasons.append("Not a scan failure; won't be fixed by retrying on this account.")
        if not has_image:
            reasons.append("Image wasn't captured on this account.")
    elif valid and has_price and (has_image or image_none):
        category = "complete"
        reasons.append("Looks complete — has price and image (or confirmed no image).")
    elif valid and name:
        category = "incomplete"
        missing: list[str] = []
        if not has_price:
            missing.append("price")
        if not has_image and not image_none:
            missing.append("image")
        if missing:
            reasons.append(
                f"Name was found via fast HTTP scan, but {' and '.join(missing)} never came back from the browser step."
            )
            reasons.append("Common cause: auto-scan concurrency too high — enrich fails silently under load.")
            fixes.append("Scan One on this code, or use Fix incomplete.")
        if image_none:
            reasons.append("Marked as no catalog image on the checkout page.")
    else:
        category = "incomplete"
        reasons.append("Valid hit but missing basic product data.")
        fixes.append("Scan One on this code.")

    log_lines: list[str] = []
    for entry in log_entries or []:
        event = entry.get("event") or "log"
        detail = entry.get("error") or entry.get("message") or entry.get("hint") or ""
        phase = entry.get("phase")
        suffix = f" ({phase})" if phase else ""
        if detail:
            log_lines.append(f"{event}{suffix}: {detail}")
        elif event not in ("extract_script_ok",):
            log_lines.append(event + suffix)

    return {
        "code": row.get("code"),
        "category": category,
        "reasons": reasons,
        "fixes": fixes,
        "log": log_lines,
        "checked_at": row.get("checked_at"),
        "status": status,
        "has_price": has_price,
        "has_image": has_image,
        "image_none": image_none,
    }


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


IMAGE_NONE_MARKER = "Image: none on checkout page"
PREREQUISITE_MARKER = "Prerequisite required to purchase"
ALREADY_OWNED_MARKER = "Scan account already owns this product"
NOT_ELIGIBLE_MARKER = "Scan account not eligible for this product"
THROTTLED_MARKER = "Retry later — Battle.net is throttling this code"
SCAN_FAILURES_RE = re.compile(r"scan_failures:\s*(\d+)", re.I)
MAX_AUTO_RETRIES = 2

_PREREQ_TEXT_RE = re.compile(
    r"first things first|you need .+ to purchase this product",
    re.I,
)
_PREREQ_MSG_RE = re.compile(r"you need\s+(.+?)\s+to purchase this product", re.I)
_ALREADY_OWNED_RE = re.compile(
    r"already have access to this product|good news[^\n]{0,80}already have access",
    re.I,
)
_NOT_ELIGIBLE_RE = re.compile(
    r"not eligible to purchase|sorry,?\s*you'?re not eligible",
    re.I,
)
_NOT_ELIGIBLE_DETAIL_RE = re.compile(
    r"sorry,?\s*you'?re not eligible to purchase\s+(.+?)(?:\.|$)",
    re.I | re.M,
)

NAME_PREREQUISITE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"reaper of souls", re.I), "You need Diablo III to purchase this product"),
    (re.compile(r"lord of destruction", re.I), "You need Diablo II to purchase this product"),
    (re.compile(r"diablo\s*iii.*upgrade", re.I), "You need Diablo III to purchase this product"),
]


def infer_prerequisite_from_name(name: str | None) -> str | None:
    if not name:
        return None
    for pattern, msg in NAME_PREREQUISITE_RULES:
        if pattern.search(name):
            return msg
    return None


def detect_prerequisite_message(text: str | None) -> str | None:
    if not text or not _PREREQ_TEXT_RE.search(text):
        return None
    match = _PREREQ_MSG_RE.search(text)
    if match:
        return f"You need {match.group(1).strip()} to purchase this product"
    for line in text.splitlines():
        line = line.strip()
        if _PREREQ_MSG_RE.search(line):
            return line
    return "Prerequisite required to purchase this product"


def detect_already_owned_message(text: str | None) -> str | None:
    if not text or not _ALREADY_OWNED_RE.search(text):
        return None
    return ALREADY_OWNED_MARKER


def detect_not_eligible_message(text: str | None) -> str | None:
    if not text or not _NOT_ELIGIBLE_RE.search(text):
        return None
    match = _NOT_ELIGIBLE_DETAIL_RE.search(text or "")
    if match:
        detail = match.group(1).strip().rstrip(".")
        if detail:
            return f"Not eligible to purchase: {detail}"
    for line in (text or "").splitlines():
        line = line.strip()
        if _NOT_ELIGIBLE_RE.search(line):
            return line if len(line) > 20 else NOT_ELIGIBLE_MARKER
    return NOT_ELIGIBLE_MARKER


def _has_prerequisite_marker(raw_notes: str | None) -> bool:
    return PREREQUISITE_MARKER.lower() in (raw_notes or "").lower()


def _has_already_owned_marker(raw_notes: str | None) -> bool:
    return ALREADY_OWNED_MARKER.lower() in (raw_notes or "").lower()


def _has_not_eligible_marker(raw_notes: str | None) -> bool:
    notes = (raw_notes or "").lower()
    return NOT_ELIGIBLE_MARKER.lower() in notes or "not eligible to purchase" in notes


def is_already_owned_entry(entry: dict[str, Any]) -> bool:
    if _has_already_owned_marker(entry.get("raw_notes")):
        return True
    msg = (entry.get("message") or "").lower()
    return "already owns this product" in msg or "already have access to this product" in msg


def is_not_eligible_entry(entry: dict[str, Any]) -> bool:
    if (entry.get("status") or "").strip() == "not_eligible":
        return True
    if _has_not_eligible_marker(entry.get("raw_notes")):
        return True
    msg = (entry.get("message") or "").lower()
    return "not eligible to purchase" in msg or "not eligible for this product" in msg


def apply_prerequisite_notes(entry: dict[str, Any]) -> dict[str, Any]:
    entry = dict(entry)
    if _has_prerequisite_marker(entry.get("raw_notes")):
        return entry
    combined = "\n".join(
        part for part in (entry.get("message"), entry.get("raw_notes")) if part
    )
    msg = detect_prerequisite_message(combined)
    if not msg:
        msg = infer_prerequisite_from_name(entry.get("name"))
    if not msg:
        return entry
    notes = (entry.get("raw_notes") or "").strip()
    if not _has_prerequisite_marker(notes):
        notes = f"{PREREQUISITE_MARKER}\n{msg}" + (f"\n{notes}" if notes else "")
    entry["raw_notes"] = notes
    entry["message"] = msg
    entry["valid"] = True
    entry["status"] = "valid"
    return entry


def apply_already_owned_notes(entry: dict[str, Any]) -> dict[str, Any]:
    entry = dict(entry)
    if _has_prerequisite_marker(entry.get("raw_notes")):
        return entry
    if _has_already_owned_marker(entry.get("raw_notes")):
        return entry
    combined = "\n".join(
        part for part in (entry.get("message"), entry.get("raw_notes")) if part
    )
    msg = detect_already_owned_message(combined)
    if not msg:
        return entry
    notes = (entry.get("raw_notes") or "").strip()
    if not _has_already_owned_marker(notes):
        notes = ALREADY_OWNED_MARKER + (f"\n{notes}" if notes else "")
    entry["raw_notes"] = notes
    entry["message"] = msg
    entry["valid"] = True
    entry["status"] = "valid"
    entry["price"] = None
    return entry


def apply_not_eligible_notes(entry: dict[str, Any]) -> dict[str, Any]:
    entry = dict(entry)
    if _has_prerequisite_marker(entry.get("raw_notes")):
        return entry
    if _has_already_owned_marker(entry.get("raw_notes")):
        return entry
    if _has_not_eligible_marker(entry.get("raw_notes")):
        return entry
    combined = "\n".join(
        part for part in (entry.get("message"), entry.get("raw_notes")) if part
    )
    msg = detect_not_eligible_message(combined)
    if not msg:
        return entry
    notes = (entry.get("raw_notes") or "").strip()
    if not _has_not_eligible_marker(notes):
        head = NOT_ELIGIBLE_MARKER if msg == NOT_ELIGIBLE_MARKER else f"{NOT_ELIGIBLE_MARKER}\n{msg}"
        notes = head + (f"\n{notes}" if notes else "")
    entry["raw_notes"] = notes
    entry["message"] = msg
    entry["valid"] = True
    entry["status"] = "not_eligible"
    entry["price"] = None
    return entry


def backfill_not_eligible_products() -> int:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT code, name, message, raw_notes FROM products
        WHERE valid = 1
          AND NOT (LOWER(IFNULL(raw_notes, '')) LIKE '%not eligible%')
          AND (
            LOWER(IFNULL(message, '')) LIKE '%not eligible%'
            OR LOWER(IFNULL(raw_notes, '')) LIKE '%not eligible%'
          )
        """
    ).fetchall()
    updated = 0
    for row in rows:
        data = dict(row)
        original_notes = data.get("raw_notes")
        patched = apply_not_eligible_notes(data)
        if patched.get("raw_notes") != original_notes or patched.get("message") != data.get("message"):
            conn.execute(
                """
                UPDATE products
                SET raw_notes = ?, message = ?, price = NULL, valid = 1, status = 'not_eligible'
                WHERE code = ?
                """,
                (patched.get("raw_notes"), patched.get("message"), data["code"]),
            )
            updated += 1
    conn.commit()
    conn.close()
    return updated


def backfill_already_owned_products() -> int:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT code, name, message, raw_notes FROM products
        WHERE valid = 1
          AND NOT (LOWER(IFNULL(raw_notes, '')) LIKE '%scan account already owns%')
          AND (
            LOWER(IFNULL(message, '')) LIKE '%already have access%'
            OR LOWER(IFNULL(raw_notes, '')) LIKE '%already have access%'
          )
        """
    ).fetchall()
    updated = 0
    for row in rows:
        data = dict(row)
        original_notes = data.get("raw_notes")
        patched = apply_already_owned_notes(data)
        if patched.get("raw_notes") != original_notes:
            conn.execute(
                """
                UPDATE products
                SET raw_notes = ?, message = ?, price = NULL, valid = 1, status = 'valid'
                WHERE code = ?
                """,
                (patched.get("raw_notes"), patched.get("message"), data["code"]),
            )
            updated += 1
    conn.commit()
    conn.close()
    return updated


def backfill_prerequisite_products() -> int:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT code, name, message, raw_notes FROM products
        WHERE valid = 1 AND IFNULL(price, '') = ''
          AND NOT (LOWER(IFNULL(raw_notes, '')) LIKE '%prerequisite required%')
        """
    ).fetchall()
    updated = 0
    for row in rows:
        data = dict(row)
        original_notes = data.get("raw_notes")
        patched = apply_prerequisite_notes(data)
        if patched.get("raw_notes") != original_notes:
            conn.execute(
                """
                UPDATE products
                SET raw_notes = ?, message = ?, valid = 1, status = 'valid'
                WHERE code = ?
                """,
                (patched.get("raw_notes"), patched.get("message"), data["code"]),
            )
            updated += 1
    conn.commit()
    conn.close()
    return updated


def backfill_game_labels() -> int:
    conn = get_connection()
    rows = conn.execute(
        "SELECT code, name, game FROM products WHERE valid = 1 AND IFNULL(name, '') != ''"
    ).fetchall()
    updated = 0
    for row in rows:
        data = dict(row)
        detected = detect_game(data.get("name"))
        if detected and detected != data.get("game"):
            conn.execute("UPDATE products SET game = ? WHERE code = ?", (detected, data["code"]))
            updated += 1
    conn.commit()
    conn.close()
    return updated


def _complete_valid_sql(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"""(
            {prefix}valid = 1
            AND IFNULL({prefix}name, '') != ''
            AND (
              (
                IFNULL({prefix}price, '') != ''
                AND (
                  IFNULL({prefix}image_path, '') != ''
                  OR IFNULL({prefix}image_url, '') != ''
                  OR LOWER(IFNULL({prefix}raw_notes, '')) LIKE '%image: none%'
                )
              )
              OR LOWER(IFNULL({prefix}raw_notes, '')) LIKE '%prerequisite required%'
              OR {_not_eligible_sql(alias)}
              OR {_already_owned_sql(alias)}
            )
          )"""


def _failed_sql(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"""(
            {prefix}valid = 0
            AND {prefix}status IN ('invalid', 'error', 'rate_limited', 'timeout', 'server_error', 'needs_login')
            AND NOT {_confirmed_empty_sql(alias)}
            AND NOT {_throttled_sql(alias)}
          )"""


def _incomplete_valid_sql(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"""(
            {prefix}valid = 1
            AND NOT ({_complete_valid_sql(alias)})
            AND NOT ({_account_gated_sql(alias)})
          )"""


def get_complete_valid_codes_in_range(start: int, end: int) -> set[int]:
    conn = get_connection()
    rows = conn.execute(
        f"""
        SELECT code FROM products
        WHERE code >= ? AND code <= ? AND {_complete_valid_sql()}
        """,
        (start, end),
    ).fetchall()
    conn.close()
    return {row[0] for row in rows}


def get_smart_skip_codes_in_range(start: int, end: int) -> set[int]:
    return get_complete_valid_codes_in_range(start, end) | get_confirmed_empty_codes_in_range(
        start, end
    )


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


def _confirmed_empty_sql(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"""(
            {prefix}status = 'empty'
            OR LOWER(IFNULL({prefix}message, '')) LIKE '%nothing here%'
            OR IFNULL({prefix}message, '') = 'Product not found'
            OR LOWER(IFNULL({prefix}message, '')) LIKE '%no product exists%'
          )"""


def get_invalid_codes_in_range(start: int, end: int) -> list[int]:
    conn = get_connection()
    rows = conn.execute(
        f"""
        SELECT code FROM products
        WHERE code >= ? AND code <= ? AND valid = 0
          AND status IN ('invalid', 'error', 'rate_limited', 'timeout', 'server_error', 'needs_login')
          AND NOT {_confirmed_empty_sql()}
        ORDER BY code
        """,
        (start, end),
    ).fetchall()
    conn.close()
    return [row[0] for row in rows]


def count_confirmed_empty() -> int:
    conn = get_connection()
    row = conn.execute(
        f"""
        SELECT COUNT(*) FROM products
        WHERE valid = 0 AND {_confirmed_empty_sql()}
        """
    ).fetchone()
    conn.close()
    return int(row[0])


def count_incomplete_valid_codes() -> int:
    conn = get_connection()
    row = conn.execute(
        f"""
        SELECT COUNT(*) FROM products
        WHERE {_incomplete_valid_sql()}
        """
    ).fetchone()
    conn.close()
    return int(row[0])


def count_not_eligible_codes() -> int:
    conn = get_connection()
    row = conn.execute(
        f"SELECT COUNT(*) FROM products WHERE {_not_eligible_sql()}"
    ).fetchone()
    conn.close()
    return int(row[0] or 0)


def count_already_owned_codes() -> int:
    conn = get_connection()
    row = conn.execute(
        f"SELECT COUNT(*) FROM products WHERE {_already_owned_sql()}"
    ).fetchone()
    conn.close()
    return int(row[0] or 0)


def count_throttled_codes() -> int:
    conn = get_connection()
    row = conn.execute(
        f"SELECT COUNT(*) FROM products WHERE {_throttled_sql()}"
    ).fetchone()
    conn.close()
    return int(row[0] or 0)


def count_failed_scans() -> int:
    conn = get_connection()
    row = conn.execute(
        f"SELECT COUNT(*) FROM products WHERE {_failed_sql()}"
    ).fetchone()
    conn.close()
    return int(row[0] or 0)


STATS_CACHE_TTL_SEC = 30.0
_stats_cache: dict[str, Any] | None = None
_stats_cache_at: float = 0.0
_stats_cache_lock = Lock()
_stats_compute_lock = Lock()


def invalidate_stats_cache() -> None:
    global _stats_cache_at
    with _stats_cache_lock:
        _stats_cache_at = 0.0


def peek_stats_cache() -> dict[str, Any] | None:
    with _stats_cache_lock:
        if _stats_cache is None:
            return None
        return dict(_stats_cache)


def stats_cache_is_stale() -> bool:
    with _stats_cache_lock:
        if _stats_cache is None:
            return True
        return (time.monotonic() - _stats_cache_at) >= STATS_CACHE_TTL_SEC


def library_stats_snapshot(*, force: bool = False) -> dict[str, Any]:
    global _stats_cache, _stats_cache_at
    now = time.monotonic()
    with _stats_cache_lock:
        if (
            not force
            and _stats_cache is not None
            and (now - _stats_cache_at) < STATS_CACHE_TTL_SEC
        ):
            return dict(_stats_cache)

    with _stats_compute_lock:
        now = time.monotonic()
        with _stats_cache_lock:
            if (
                not force
                and _stats_cache is not None
                and (now - _stats_cache_at) < STATS_CACHE_TTL_SEC
            ):
                return dict(_stats_cache)

        snapshot = {
            "library_count": count_products(),
            "valid_count": count_products(valid_only=True),
            "incomplete_count": count_incomplete_valid_codes(),
            "failed_count": count_failed_scans(),
            "not_eligible_count": count_not_eligible_codes(),
            "throttled_count": count_throttled_codes(),
            "already_owned_count": count_already_owned_codes(),
            "empty_count": count_confirmed_empty(),
            "games": list_games(valid_only=True),
        }
        with _stats_cache_lock:
            _stats_cache = snapshot
            _stats_cache_at = now
        return dict(snapshot)


def get_all_failed_codes() -> list[int]:
    conn = get_connection()
    rows = conn.execute(
        f"""
        SELECT code FROM products
        WHERE valid = 0
          AND status IN ('invalid', 'error', 'rate_limited', 'timeout', 'server_error', 'needs_login')
          AND NOT {_confirmed_empty_sql()}
          AND NOT {_throttled_sql()}
        ORDER BY code
        """
    ).fetchall()
    conn.close()
    return [row[0] for row in rows]


def get_already_owned_codes() -> set[int]:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT code FROM products
        WHERE valid = 1 AND (
          LOWER(IFNULL(raw_notes, '')) LIKE '%scan account already owns%'
          OR LOWER(IFNULL(message, '')) LIKE '%already owns this product%'
        )
        """
    ).fetchall()
    conn.close()
    return {row[0] for row in rows}


def get_not_eligible_codes() -> set[int]:
    conn = get_connection()
    rows = conn.execute(
        f"SELECT code FROM products WHERE {_not_eligible_sql()}"
    ).fetchall()
    conn.close()
    return {row[0] for row in rows}


def backfill_throttled_products() -> int:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT code, raw_notes, status, message FROM products
        WHERE valid = 0
          AND status IN ('timeout', 'rate_limited')
          AND IFNULL(status, '') != 'throttled'
        """
    ).fetchall()
    updated = 0
    for row in rows:
        data = dict(row)
        merged = {**data, "valid": False}
        merged = _apply_scan_failure_tracking(data, merged)
        if merged.get("status") != "throttled":
            merged = _apply_scan_failure_tracking({"raw_notes": merged.get("raw_notes")}, merged)
        if (
            merged.get("status") != data.get("status")
            or merged.get("message") != data.get("message")
            or merged.get("raw_notes") != data.get("raw_notes")
        ):
            conn.execute(
                """
                UPDATE products SET status = ?, message = ?, raw_notes = ? WHERE code = ?
                """,
                (merged.get("status"), merged.get("message"), merged.get("raw_notes"), data["code"]),
            )
            updated += 1
    conn.commit()
    conn.close()
    return updated


def backfill_not_eligible_status() -> int:
    conn = get_connection()
    cur = conn.execute(
        f"""
        UPDATE products
        SET status = 'not_eligible'
        WHERE {_not_eligible_sql()} AND IFNULL(status, '') != 'not_eligible'
        """
    )
    updated = cur.rowcount
    conn.commit()
    conn.close()
    return updated


def get_throttled_codes() -> set[int]:
    conn = get_connection()
    rows = conn.execute(f"SELECT code FROM products WHERE {_throttled_sql()}").fetchall()
    conn.close()
    return {row[0] for row in rows}


def get_throttled_codes_in_range(start: int, end: int) -> list[int]:
    conn = get_connection()
    rows = conn.execute(
        f"""
        SELECT code FROM products
        WHERE code >= ? AND code <= ? AND {_throttled_sql()}
        ORDER BY code
        """,
        (start, end),
    ).fetchall()
    conn.close()
    return [row[0] for row in rows]


def reset_throttle_for_retry(codes: list[int]) -> int:
    if not codes:
        return 0
    conn = get_connection()
    placeholders = ",".join("?" * len(codes))
    rows = conn.execute(
        f"SELECT code, raw_notes FROM products WHERE code IN ({placeholders}) AND {_throttled_sql()}",
        codes,
    ).fetchall()
    updated = 0
    for row in rows:
        notes = SCAN_FAILURES_RE.sub("", row["raw_notes"] or "").strip()
        notes = notes.replace(THROTTLED_MARKER, "").strip() or None
        conn.execute(
            """
            UPDATE products
            SET status = 'timeout', message = NULL, raw_notes = ?
            WHERE code = ?
            """,
            (notes, row["code"]),
        )
        updated += 1
    conn.commit()
    conn.close()
    if updated:
        invalidate_stats_cache()
    return updated


def get_account_gated_codes() -> set[int]:
    return get_already_owned_codes() | get_not_eligible_codes() | get_throttled_codes()


def get_fix_queue_codes(start: int, end: int) -> list[int]:
    gated = get_account_gated_codes()
    failed = set(get_failed_codes_in_range(start, end)) - gated
    incomplete = {
        code for code in get_incomplete_valid_codes(for_fix=True) if start <= code <= end
    } - gated
    return sorted(failed | incomplete)


def get_failed_codes_in_range(start: int, end: int) -> list[int]:
    conn = get_connection()
    rows = conn.execute(
        f"""
        SELECT code FROM products
        WHERE code >= ? AND code <= ? AND valid = 0
          AND status IN ('invalid', 'error', 'rate_limited', 'timeout', 'server_error', 'needs_login')
          AND NOT {_confirmed_empty_sql()}
        ORDER BY code
        """,
        (start, end),
    ).fetchall()
    conn.close()
    return [row[0] for row in rows]


def get_incomplete_valid_codes(*, for_fix: bool = False) -> list[int]:
    conn = get_connection()
    rows = conn.execute(
        f"""
        SELECT code FROM products
        WHERE valid = 1
          AND NOT ({_complete_valid_sql()})
        ORDER BY code
        """
    ).fetchall()
    conn.close()
    codes = [row[0] for row in rows]
    if not for_fix:
        return codes
    gated = get_account_gated_codes()
    return [code for code in codes if code not in gated]


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


def _delete_product_images(code: int) -> None:
    image_path = IMAGES_DIR / f"{code}.jpg"
    if image_path.exists():
        image_path.unlink()
    for ext in (".png", ".webp", ".jpeg"):
        alt = IMAGES_DIR / f"{code}{ext}"
        if alt.exists():
            alt.unlink()


def delete_product(code: int) -> bool:
    conn = get_connection()
    cur = conn.execute("DELETE FROM products WHERE code = ?", (code,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    if deleted:
        _delete_product_images(code)
    return deleted


def delete_products_in_range(start_code: int, end_code: int) -> int:
    start = min(int(start_code), int(end_code))
    end = max(int(start_code), int(end_code))
    conn = get_connection()
    rows = conn.execute(
        "SELECT code FROM products WHERE code >= ? AND code <= ?",
        (start, end),
    ).fetchall()
    codes = [int(row["code"]) for row in rows]
    if not codes:
        conn.close()
        return 0
    cur = conn.execute(
        "DELETE FROM products WHERE code >= ? AND code <= ?",
        (start, end),
    )
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    for code in codes:
        _delete_product_images(code)
    return deleted


def load_settings() -> dict[str, Any]:
    ensure_dirs()
    default = {
        "region": "us",
        "start_code": 0,
        "end_code": 1000000,
        "current_code": 0,
        "delay_ms": 2000,
        "concurrency": 2,
        "skip_scanned": True,
        "skip_no_product": False,
        "headless": True,
        "active_database": "library",
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


def problems_json_path(name: str | None = None) -> Path:
    db_name = sanitize_database_name(name or active_database_name())
    return DATABASES_DIR / f"{db_name}-problems.json"


def _missing_product_fields(row: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    if not _has_value(row.get("price")):
        missing.append("price")
    if not _has_value(row.get("image_path")) and not _has_value(row.get("image_url")):
        missing.append("image")
    if not _has_value(row.get("game")):
        missing.append("game")
    return missing


def build_problems_payload() -> dict[str, Any]:
    conn = get_connection()
    failed_rows = conn.execute(
        f"""
        SELECT code, status, message, url, checked_at
        FROM products
        WHERE {_failed_sql()}
        ORDER BY code
        """
    ).fetchall()
    incomplete_rows = conn.execute(
        f"""
        SELECT code, name, price, image_url, image_path, game, status, message, url, checked_at
        FROM products
        WHERE {_incomplete_valid_sql()}
        ORDER BY code
        """
    ).fetchall()
    not_eligible_rows = conn.execute(
        f"""
        SELECT code, name, message, url, checked_at, raw_notes
        FROM products
        WHERE {_not_eligible_sql()}
        ORDER BY code
        """
    ).fetchall()
    conn.close()

    failed = [_normalize_row(row) for row in failed_rows]
    incomplete_raw = [_normalize_row(row) for row in incomplete_rows]
    not_eligible = [_normalize_row(row) for row in not_eligible_rows]
    incomplete = [
        {**row, "missing_fields": _missing_product_fields(row)}
        for row in incomplete_raw
    ]
    failed_codes = [row["code"] for row in failed]
    incomplete_codes = [row["code"] for row in incomplete]
    not_eligible_codes = [row["code"] for row in not_eligible]

    return {
        "database": active_database_name(),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "failed_count": len(failed_codes),
            "incomplete_count": len(incomplete_codes),
            "not_eligible_count": len(not_eligible_codes),
            "total_problems": len(failed_codes) + len(incomplete_codes),
        },
        "failed_codes": failed_codes,
        "incomplete_codes": incomplete_codes,
        "not_eligible_codes": not_eligible_codes,
        "failed": failed,
        "incomplete": incomplete,
        "not_eligible": not_eligible,
    }


def export_problems_json(*, name: str | None = None) -> Path:
    ensure_dirs()
    payload = build_problems_payload()
    path = problems_json_path(name)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def export_library() -> list[dict[str, Any]]:
    return list_products()


def build_catalog_payload() -> dict[str, Any]:
    items = [item for item in export_library() if item.get("valid")]
    return {
        "products": len(items),
        "valid_count": len(items),
        "library_count": count_products(),
        "incomplete_count": count_incomplete_valid_codes(),
        "failed_count": count_failed_scans(),
        "not_eligible_count": count_not_eligible_codes(),
        "throttled_count": count_throttled_codes(),
        "already_owned_count": count_already_owned_codes(),
        "empty_count": count_confirmed_empty(),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }


def publish_catalog_snapshots(extra_paths: list[Path] | None = None) -> list[tuple[Path, int]]:
    backfill_auto_catalog_tags()
    payload = build_catalog_payload()
    text = json.dumps(payload, indent=2)
    count = int(payload["valid_count"])
    targets = [DATA_DIR.parent / "static" / "catalog.json"]
    if extra_paths:
        targets.extend(extra_paths)
    written: list[tuple[Path, int]] = []
    for path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        written.append((path, count))
    return written


def backup_library() -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_dir = DATA_DIR / "backups" / ts
    backup_dir.mkdir(parents=True, exist_ok=True)
    if get_db_path().exists():
        shutil.copy2(get_db_path(), backup_dir / "library.db")
    payload = build_catalog_payload()
    (backup_dir / "catalog.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (backup_dir / "library-full.json").write_text(
        json.dumps(export_library(), indent=2), encoding="utf-8"
    )
    settings = load_settings()
    (backup_dir / "settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return backup_dir


def reset_library_db(*, reset_scan_cursor: bool = True) -> None:
    conn = get_connection()
    conn.execute("DELETE FROM products")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    if reset_scan_cursor:
        settings = load_settings()
        start = int(settings.get("start_code") or 0)
        settings["current_code"] = start
        save_settings(settings)
