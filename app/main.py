import asyncio
import json
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.library import (
    active_database_name,
    count_products,
    count_confirmed_empty,
    count_failed_scans,
    count_incomplete_valid_codes,
    count_not_eligible_codes,
    count_already_owned_codes,
    count_throttled_codes,
    backfill_throttled_products,
    backfill_prerequisite_products,
    backfill_already_owned_products,
    backfill_not_eligible_products,
    backfill_not_eligible_status,
    backfill_game_labels,
    backup_library,
    create_database,
    database_info,
    invalidate_stats_cache,
    list_backup_snapshots,
    list_databases,
    publish_catalog_snapshots,
    reset_library_db,
    restore_database_from_backup,
    switch_database,
    delete_product,
    delete_products_in_range,
    ensure_dirs,
    export_library,
    export_problems_json,
    explain_product,
    problems_json_path,
    get_product,
    get_failed_codes_in_range,
    get_throttled_codes_in_range,
    reset_throttle_for_retry,
    get_fix_queue_codes,
    get_incomplete_valid_codes,
    get_scanned_codes_in_range,
    get_skip_codes_in_range,
    get_smart_skip_codes_in_range,
    list_games,
    list_products,
    list_recent_products,
    library_stats_snapshot,
    load_settings,
    peek_stats_cache,
    stats_cache_is_stale,
    next_unscanned_code,
    save_settings,
    upsert_product,
)
from app.scanner import BattleNetScanner, ScanResult, SCANNER_VERSION, ScanStatus
from app import scan_debug

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"

app = FastAPI(title="Battle.net Code Library")
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://(.*\.)?bore\.rip|http://localhost(:\d+)?|http://127\.0\.0\.1(:\d+)?",
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def private_network_access(request: Request, call_next):
    if request.method == "OPTIONS":
        response: Response = await call_next(request)
    else:
        response = await call_next(request)
    if request.headers.get("access-control-request-private-network") is not None:
        response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response
scanner = BattleNetScanner()
websocket_clients: set[WebSocket] = set()
_auto_current_code: int | None = None
_background_tasks: set[asyncio.Task] = set()
BORE_RIP_CATALOG = ROOT.parent / "bore.rip" / "public" / "data" / "battlenet-catalog.json"
_catalog_export_scheduled = False
_catalog_export_lock = asyncio.Lock()
NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
}


def write_bore_rip_catalog_snapshot() -> None:
    extra: list[Path] = []
    if BORE_RIP_CATALOG.parent.exists():
        extra.append(BORE_RIP_CATALOG)
    publish_catalog_snapshots(extra)


async def schedule_bore_rip_catalog_export() -> None:
    global _catalog_export_scheduled
    if not BORE_RIP_CATALOG.parent.exists():
        return
    async with _catalog_export_lock:
        if _catalog_export_scheduled:
            return
        _catalog_export_scheduled = True

    await asyncio.sleep(12)

    async with _catalog_export_lock:
        _catalog_export_scheduled = False

    try:
        await asyncio.to_thread(write_bore_rip_catalog_snapshot)
    except Exception:
        pass


def _track_background(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


_stats_async_lock = asyncio.Lock()
_stats_refresh_task: asyncio.Task | None = None


async def _compute_stats_snapshot() -> dict[str, Any]:
    async with _stats_async_lock:
        if not stats_cache_is_stale():
            cached = peek_stats_cache()
            if cached is not None:
                return cached
        return await asyncio.to_thread(library_stats_snapshot)


def _schedule_stats_refresh() -> None:
    global _stats_refresh_task
    if _stats_refresh_task and not _stats_refresh_task.done():
        return
    _stats_refresh_task = _track_background(_compute_stats_snapshot())


async def get_status_stats() -> dict[str, Any]:
    cached = peek_stats_cache()
    if cached is not None:
        if stats_cache_is_stale():
            _schedule_stats_refresh()
        return cached
    return await _compute_stats_snapshot()


class SettingsUpdate(BaseModel):
    region: str | None = None
    start_code: int | None = None
    end_code: int | None = None
    current_code: int | None = None
    delay_ms: int | None = Field(default=None, ge=0, le=600000)
    concurrency: int | None = Field(default=None, ge=1)
    skip_scanned: bool | None = None
    skip_no_product: bool | None = None
    headless: bool | None = None


class ScanRequest(BaseModel):
    code: int
    region: str = "us"
    headless: bool = True


class AutoScanRequest(BaseModel):
    start_code: int
    end_code: int
    delay_ms: int = Field(default=2000, ge=0, le=600000)
    concurrency: int = Field(default=2, ge=1)
    skip_scanned: bool = True
    skip_no_product: bool = False
    region: str = "us"
    headless: bool = True


class SmartScanRequest(BaseModel):
    start_code: int
    end_code: int
    delay_ms: int = Field(default=0, ge=0, le=600000)
    concurrency: int = Field(default=20, ge=1)
    region: str = "us"
    headless: bool = True


class RevalidateRequest(BaseModel):
    start_code: int
    end_code: int
    delay_ms: int = Field(default=0, ge=0, le=600000)
    concurrency: int = Field(default=4, ge=1)
    region: str = "us"
    headless: bool = True


class DeleteRangeRequest(BaseModel):
    start_code: int = Field(ge=0)
    end_code: int = Field(ge=0)


class DatabaseCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    switch: bool = True


class DatabaseSwitchRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class DatabaseRestoreRequest(BaseModel):
    backup_id: str = Field(min_length=1, max_length=128)
    target_name: str | None = Field(default=None, max_length=64)
    restore_settings: bool = True


def _scanner_busy() -> bool:
    return scanner.status in (ScanStatus.RUNNING, ScanStatus.PAUSED)


def _scan_busy_error() -> dict[str, Any]:
    return {"ok": False, "error": "Stop the running scan before changing databases."}


def _normalize_scan_load(*, delay_ms: int, concurrency: int) -> tuple[int, int]:
    safe_concurrency = max(1, min(concurrency, 12))
    safe_delay = max(0, delay_ms)
    if safe_concurrency > 8 and safe_delay < 50:
        safe_delay = 50
    return safe_delay, safe_concurrency


def _gentle_fix_scan_kwargs(body: RevalidateRequest, *, enrich_only_codes: set[int] | None = None) -> dict[str, Any]:
    safe_delay, safe_concurrency = _normalize_scan_load(
        delay_ms=body.delay_ms,
        concurrency=body.concurrency,
    )
    gentle_workers = max(1, min(safe_concurrency, 8))
    return {
        "delay_ms": max(safe_delay, 400),
        "concurrency": gentle_workers,
        "region": body.region,
        "headless": body.headless,
        "browser_only": True,
        "gentle_browser": True,
        "enrich_only_codes": enrich_only_codes or set(),
    }


async def _refresh_problems_json() -> None:
    await asyncio.to_thread(export_problems_json)


async def broadcast(event: str, payload: dict[str, Any]) -> None:
    dead: list[WebSocket] = []
    message = json.dumps({"event": event, "payload": payload})
    for ws in list(websocket_clients):
        try:
            await ws.send_text(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        websocket_clients.discard(ws)
    if event == "library_updated":
        _track_background(schedule_bore_rip_catalog_export())


async def persist_result(result: ScanResult, *, force: bool = False, notify_library: bool = True) -> dict[str, Any]:
    saved = upsert_product(result.to_dict(), force=force)
    if notify_library:
        await broadcast("library_updated", {"code": result.code})
    return saved


async def scanner_event_handler(event: str, payload: dict[str, Any]) -> None:
    await broadcast(event, payload)
    if event == "auto_finished":
        _track_background(_refresh_problems_json())
        if _auto_current_code is not None:
            save_settings({"current_code": _auto_current_code})


@app.on_event("startup")
async def startup_event() -> None:
    ensure_dirs()
    scanner.set_event_handler(scanner_event_handler)
    scanner._schedule_extract_validation()
    _track_background(_refresh_problems_json())
    _schedule_stats_refresh()
    _track_background(_run_startup_backfills())


async def _run_startup_backfills() -> None:
    prereq = await asyncio.to_thread(backfill_prerequisite_products)
    owned = await asyncio.to_thread(backfill_already_owned_products)
    ineligible = await asyncio.to_thread(backfill_not_eligible_products)
    ineligible_status = await asyncio.to_thread(backfill_not_eligible_status)
    throttled = await asyncio.to_thread(backfill_throttled_products)
    games = await asyncio.to_thread(backfill_game_labels)
    if prereq:
        print(f"Marked {prereq} prerequisite-gated products as complete")
    if owned:
        print(f"Marked {owned} already-owned products")
    if ineligible:
        print(f"Marked {ineligible} not-eligible products")
    if ineligible_status:
        print(f"Tagged {ineligible_status} rows with not_eligible status")
    if throttled:
        print(f"Tagged {throttled} throttled codes (stop auto-retry)")
    if games:
        print(f"Fixed {games} product game labels")
    invalidate_stats_cache()
    _schedule_stats_refresh()


@app.get("/api/debug/log")
async def api_debug_log(limit: int = 150) -> dict[str, Any]:
    cap = min(max(limit, 1), 250)
    return {
        "path": str(scan_debug.LOG_PATH),
        "entries": scan_debug.recent(cap),
        **scanner.extract_script_status(),
    }


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await scanner.close()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
async def browse_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "browse.html", headers=NO_CACHE_HEADERS)


@app.get("/browse")
async def browse_alias() -> FileResponse:
    return FileResponse(STATIC_DIR / "browse.html", headers=NO_CACHE_HEADERS)


@app.get("/scanner")
async def scanner_page() -> Response:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = re.sub(
        r'(static/app\.js\?v=)[^"\']+',
        rf"\g<1>{SCANNER_VERSION}",
        html,
    )
    html = re.sub(
        r'(static/style\.css\?v=)[^"\']+',
        rf"\g<1>{SCANNER_VERSION}",
        html,
    )
    return Response(content=html, media_type="text/html", headers=NO_CACHE_HEADERS)


@app.get("/static/app.js")
async def scanner_app_js() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "app.js",
        media_type="application/javascript",
        headers=NO_CACHE_HEADERS,
    )


@app.get("/static/style.css")
async def scanner_style_css() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "style.css",
        media_type="text/css",
        headers=NO_CACHE_HEADERS,
    )


@app.get("/api/public/catalog")
async def api_public_catalog() -> dict[str, Any]:
    items = [item for item in export_library() if item.get("valid")]
    return {"products": len(items), "items": items}


@app.get("/api/public/stats")
async def api_public_stats() -> dict[str, Any]:
    stats = await get_status_stats()
    return {
        "products": stats["valid_count"],
        "valid_count": stats["valid_count"],
        "library_count": stats["library_count"],
        "incomplete_count": stats["incomplete_count"],
        "failed_count": stats["failed_count"],
        "not_eligible_count": stats["not_eligible_count"],
        "throttled_count": stats["throttled_count"],
        "already_owned_count": stats["already_owned_count"],
        "empty_count": stats["empty_count"],
        "games": stats["games"],
    }


@app.get("/api/public/library")
async def api_public_library(
    search: str = "",
    game: str = "",
    sort: str = "name",
    limit: int = 48,
    offset: int = 0,
) -> dict[str, Any]:
    safe_limit = min(max(limit, 1), 100)
    safe_offset = max(0, offset)
    safe_sort = sort if sort in ("code", "code_desc", "game", "game_desc", "name", "checked") else "name"

    def _load() -> dict[str, Any]:
        items = list_products(
            valid_only=True,
            search=search,
            game=game,
            sort=safe_sort,
            limit=safe_limit,
            offset=safe_offset,
        )
        return {
            "items": items,
            "total": count_products(valid_only=True, search=search, game=game),
            "limit": safe_limit,
            "offset": safe_offset,
        }

    return await asyncio.to_thread(_load)


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    settings = load_settings()
    active = active_database_name()
    stats = await get_status_stats()
    return {
        "scanner_status": scanner.status.value,
        "scanner_version": SCANNER_VERSION,
        "logged_in": scanner.is_logged_in,
        "settings": settings,
        "database": database_info(active),
        "problems_json": str(problems_json_path(active)),
        "debug": {
            **scanner.extract_script_status(),
            "recent": scan_debug.recent(8),
        },
        **stats,
        **scanner.activity_snapshot(),
    }


@app.get("/api/settings")
async def api_get_settings() -> dict[str, Any]:
    return load_settings()


@app.put("/api/settings")
async def api_update_settings(body: SettingsUpdate) -> dict[str, Any]:
    updates = body.model_dump(exclude_none=True)
    if "delay_ms" in updates or "concurrency" in updates:
        current = load_settings()
        delay_ms, concurrency = _normalize_scan_load(
            delay_ms=int(updates.get("delay_ms", current.get("delay_ms", 0))),
            concurrency=int(updates.get("concurrency", current.get("concurrency", 2))),
        )
        updates["delay_ms"] = delay_ms
        updates["concurrency"] = concurrency
    return save_settings(updates)


@app.get("/api/databases")
async def api_list_databases() -> dict[str, Any]:
    active = active_database_name()
    databases = list_databases()
    stats = await get_status_stats()
    for db in databases:
        if db["name"] == active:
            db["failed_count"] = stats["failed_count"]
            db["incomplete_count"] = stats["incomplete_count"]
            break
    return {
        "active": active,
        "databases": databases,
        "backups": list_backup_snapshots(),
        "problems_json": str(problems_json_path(active)),
    }


@app.get("/api/library/problems")
async def api_library_problems(refresh: bool = False) -> dict[str, Any]:
    if refresh:
        path = await asyncio.to_thread(export_problems_json)
    else:
        path = problems_json_path()
        if not path.exists():
            path = await asyncio.to_thread(export_problems_json)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {"path": str(path), **payload}


@app.post("/api/databases/switch")
async def api_switch_database(body: DatabaseSwitchRequest) -> dict[str, Any]:
    if _scanner_busy():
        return _scan_busy_error()
    try:
        info = await asyncio.to_thread(switch_database, body.name)
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc)}
    extra = [BORE_RIP_CATALOG] if BORE_RIP_CATALOG.parent.exists() else []
    await asyncio.to_thread(publish_catalog_snapshots, extra)
    await _refresh_problems_json()
    await broadcast("library_updated", {"database": info["name"]})
    return {"ok": True, "database": info, "problems_json": str(problems_json_path(info["name"]))}


@app.post("/api/databases/create")
async def api_create_database(body: DatabaseCreateRequest) -> dict[str, Any]:
    if _scanner_busy():
        return _scan_busy_error()
    try:
        info = await asyncio.to_thread(create_database, body.name, switch=body.switch)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if body.switch:
        extra = [BORE_RIP_CATALOG] if BORE_RIP_CATALOG.parent.exists() else []
        await asyncio.to_thread(publish_catalog_snapshots, extra)
        await _refresh_problems_json()
        await broadcast("library_updated", {"database": info["name"]})
    return {
        "ok": True,
        "database": info,
        "problems_json": str(problems_json_path(info["name"])),
    }


@app.post("/api/databases/restore")
async def api_restore_database(body: DatabaseRestoreRequest) -> dict[str, Any]:
    if _scanner_busy():
        return _scan_busy_error()
    try:
        result = await asyncio.to_thread(
            restore_database_from_backup,
            body.backup_id,
            target_name=body.target_name,
            switch=True,
            restore_settings=body.restore_settings,
        )
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc)}
    extra = [BORE_RIP_CATALOG] if BORE_RIP_CATALOG.parent.exists() else []
    await asyncio.to_thread(publish_catalog_snapshots, extra)
    await _refresh_problems_json()
    await broadcast("library_updated", {"database": result["database"]["name"], "restored": True})
    return {
        "ok": True,
        **result,
        "problems_json": str(problems_json_path(result["database"]["name"])),
    }


@app.get("/api/library/recent")
async def api_library_recent(limit: int = 5) -> list[dict[str, Any]]:
    cap = min(max(limit, 1), 20)
    return await asyncio.to_thread(list_recent_products, cap)


@app.get("/api/library-count")
async def api_library_count(
    valid_only: bool = False,
    failed_only: bool = False,
    incomplete_only: bool = False,
    not_eligible_only: bool = False,
    search: str = "",
    game: str = "",
) -> dict[str, int]:
    count = await asyncio.to_thread(
        count_products,
        valid_only=valid_only,
        failed_only=failed_only,
        incomplete_only=incomplete_only,
        not_eligible_only=not_eligible_only,
        search=search,
        game=game,
    )
    return {"count": count}


@app.get("/api/library")
async def api_library(
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
    safe_limit = None
    if limit is not None:
        safe_limit = min(max(limit, 1), 500)
    return await asyncio.to_thread(
        list_products,
        valid_only=valid_only,
        failed_only=failed_only,
        incomplete_only=incomplete_only,
        not_eligible_only=not_eligible_only,
        search=search,
        game=game,
        sort=sort,
        limit=safe_limit,
        offset=max(0, offset),
    )


@app.get("/api/library/export/json")
async def api_export_library() -> list[dict[str, Any]]:
    return export_library()


@app.delete("/api/library-range")
async def api_delete_library_range(
    start_code: int = Query(ge=0),
    end_code: int = Query(ge=0),
) -> dict[str, Any]:
    deleted = await asyncio.to_thread(delete_products_in_range, start_code, end_code)
    await broadcast("library_updated", {"deleted_range": True, "deleted": deleted})
    return {"ok": True, "deleted": deleted}


@app.post("/api/library-range")
async def api_delete_library_range_post(body: DeleteRangeRequest) -> dict[str, Any]:
    deleted = await asyncio.to_thread(delete_products_in_range, body.start_code, body.end_code)
    await broadcast("library_updated", {"deleted_range": True, "deleted": deleted})
    return {"ok": True, "deleted": deleted}


@app.get("/api/library/{code}")
async def api_get_library_item(code: int) -> dict[str, Any]:
    item = get_product(code)
    if not item:
        return {"error": "not_found"}
    return item


@app.get("/api/library/{code}/debug")
async def api_library_item_debug(code: int) -> dict[str, Any]:
    item = get_product(code)
    if not item:
        return {"error": "not_found"}
    logs = scan_debug.entries_for_code(code)
    return explain_product(item, log_entries=logs)


@app.delete("/api/library/{code}")
async def api_delete_library_item(code: int) -> dict[str, Any]:
    deleted = delete_product(code)
    await broadcast("library_updated", {"code": code, "deleted": deleted})
    return {"deleted": deleted}


@app.post("/api/catalog/publish")
async def api_publish_catalog() -> dict[str, Any]:
    extra: list[Path] = []
    if BORE_RIP_CATALOG.parent.exists():
        extra.append(BORE_RIP_CATALOG)
    written = await asyncio.to_thread(publish_catalog_snapshots, extra)
    return {
        "ok": True,
        "files": [{"path": str(path), "valid_count": count} for path, count in written],
    }


@app.post("/api/library/backup-reset")
async def api_backup_reset_library() -> dict[str, Any]:
    backup_dir = await asyncio.to_thread(backup_library)
    published = await asyncio.to_thread(publish_catalog_snapshots, [BORE_RIP_CATALOG] if BORE_RIP_CATALOG.parent.exists() else [])
    await asyncio.to_thread(reset_library_db)
    await broadcast("library_updated", {"reset": True})
    return {
        "ok": True,
        "backup_dir": str(backup_dir),
        "published": [{"path": str(p), "valid_count": c} for p, c in published],
        "message": "Database reset. Scan cursor moved to start_code.",
    }


@app.post("/api/login/open")
async def api_open_login() -> dict[str, Any]:
    try:
        channel = await scanner.open_login_browser()
    except RuntimeError as exc:
        return {"ok": False, "error": str(exc)}
    warning = None
    if channel == "chromium":
        warning = (
            "Opened bundled Chromium — Google sign-in often blocks this. "
            "Install Google Chrome and click Open Login again."
        )
    return {"ok": True, "browser": channel, "warning": warning}


@app.post("/api/login/save")
async def api_save_login() -> dict[str, Any]:
    saved = await scanner.save_login_session()
    await broadcast("login_saved", {"saved": saved})
    return {"saved": saved}


@app.post("/api/scan/next")
async def api_scan_next() -> dict[str, Any]:
    settings = load_settings()
    code = settings["current_code"]
    end_code = settings["end_code"]
    skip_scanned = settings.get("skip_scanned", True)
    skip_no_product = settings.get("skip_no_product", False)

    if skip_scanned or skip_no_product:
        next_code = next_unscanned_code(
            code,
            end_code,
            skip_valid=skip_scanned,
            skip_no_product=skip_no_product,
        )
        if next_code is None:
            save_settings({"current_code": end_code + 1})
            return {"done": True, "message": "Reached end code."}
        code = next_code
    elif code > end_code:
        return {"done": True, "message": "Reached end code."}

    save_settings({"current_code": code})

    result = await scanner.scan_code(
        code,
        region=settings.get("region", "us"),
        headless=settings.get("headless", True),
    )
    saved = await persist_result(result, force=True)
    next_code = code + 1
    if skip_scanned or skip_no_product:
        following = next_unscanned_code(
            next_code,
            end_code,
            skip_valid=skip_scanned,
            skip_no_product=skip_no_product,
        )
        next_code = following if following is not None else end_code + 1
    save_settings({"current_code": next_code})
    await broadcast("scan_result", saved)
    await broadcast("progress", {"current_code": code, "next_code": next_code, **scanner.activity_snapshot()})
    return {"result": saved, "next_code": next_code, "done": next_code > end_code}


@app.post("/api/scan/one")
async def api_scan_one(body: ScanRequest) -> dict[str, Any]:
    use_fast = scanner.status != ScanStatus.RUNNING
    prev_fast = scanner._fast_scan
    if use_fast:
        scanner._fast_scan = True
    try:
        result = await scanner.scan_code(body.code, region=body.region, headless=body.headless)
    finally:
        if use_fast:
            scanner._fast_scan = prev_fast
    saved = await persist_result(result, force=True)
    await broadcast("scan_result", saved)
    return {"result": saved}


@app.post("/api/scan/auto/start")
async def api_auto_start(body: AutoScanRequest) -> dict[str, Any]:
    global _auto_current_code
    delay_ms, concurrency = _normalize_scan_load(delay_ms=body.delay_ms, concurrency=body.concurrency)
    save_settings(
        {
            "start_code": body.start_code,
            "end_code": body.end_code,
            "current_code": body.start_code,
            "delay_ms": delay_ms,
            "concurrency": concurrency,
            "skip_scanned": body.skip_scanned,
            "skip_no_product": body.skip_no_product,
            "region": body.region,
            "headless": body.headless,
        }
    )
    _auto_current_code = body.start_code
    settings_save_counter = 0

    async def on_result(result: ScanResult) -> None:
        nonlocal settings_save_counter
        global _auto_current_code
        saved = await asyncio.to_thread(
            upsert_product, result.to_dict(), force=False, merge_missing=True
        )
        _auto_current_code = max(_auto_current_code or 0, result.code + 1)
        settings_save_counter += 1
        if result.valid or settings_save_counter % 8 == 0:
            save_settings({"current_code": _auto_current_code})
        await broadcast("scan_result", saved)
        if result.valid:
            await broadcast("library_updated", {"code": result.code})

    async def on_enriched(result: ScanResult) -> None:
        saved = await asyncio.to_thread(
            upsert_product, result.to_dict(), force=False, merge_missing=True
        )
        await broadcast("scan_result", saved)
        await broadcast("library_updated", {"code": result.code})

    skip_codes = get_skip_codes_in_range(
        body.start_code,
        body.end_code,
        skip_valid=body.skip_scanned,
        skip_no_product=body.skip_no_product,
    )

    try:
        await scanner.start_auto(
            start_code=body.start_code,
            end_code=body.end_code,
            delay_ms=delay_ms,
            region=body.region,
            headless=body.headless,
            concurrency=concurrency,
            skip_scanned=body.skip_scanned or body.skip_no_product,
            scanned_codes=skip_codes,
            on_result=on_result,
            on_enriched=on_enriched,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    await broadcast("auto_started", body.model_dump())
    return {"ok": True, "started": True}


@app.post("/api/scan/smart/start")
async def api_smart_start(body: SmartScanRequest) -> dict[str, Any]:
    """Scan forward from start→end: skip complete valids and confirmed empty, fix the rest."""
    global _auto_current_code
    if body.end_code < body.start_code:
        return {"ok": False, "error": "End code must be >= start code."}

    delay_ms, concurrency = _normalize_scan_load(delay_ms=body.delay_ms, concurrency=body.concurrency)
    save_settings(
        {
            "start_code": body.start_code,
            "end_code": body.end_code,
            "current_code": body.start_code,
            "delay_ms": delay_ms,
            "concurrency": concurrency,
            "region": body.region,
            "headless": body.headless,
        }
    )
    _auto_current_code = body.start_code
    settings_save_counter = 0

    async def on_result(result: ScanResult) -> None:
        nonlocal settings_save_counter
        global _auto_current_code
        saved = await asyncio.to_thread(
            upsert_product, result.to_dict(), force=False, merge_missing=True
        )
        _auto_current_code = max(_auto_current_code or 0, result.code + 1)
        settings_save_counter += 1
        if result.valid or settings_save_counter % 8 == 0:
            save_settings({"current_code": _auto_current_code})
        await broadcast("scan_result", saved)
        if result.valid:
            await broadcast("library_updated", {"code": result.code})

    async def on_enriched(result: ScanResult) -> None:
        saved = await asyncio.to_thread(
            upsert_product, result.to_dict(), force=False, merge_missing=True
        )
        await broadcast("scan_result", saved)
        await broadcast("library_updated", {"code": result.code})

    skip_codes = get_smart_skip_codes_in_range(body.start_code, body.end_code)

    try:
        await scanner.start_auto(
            start_code=body.start_code,
            end_code=body.end_code,
            delay_ms=delay_ms,
            region=body.region,
            headless=body.headless,
            concurrency=concurrency,
            skip_scanned=True,
            scanned_codes=skip_codes,
            on_result=on_result,
            on_enriched=on_enriched,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    await broadcast("auto_started", {**body.model_dump(), "mode": "smart"})
    return {"ok": True, "started": True, "skipped": len(skip_codes)}


@app.post("/api/scan/auto/pause")
async def api_auto_pause() -> dict[str, Any]:
    await scanner.pause_auto()
    return {"ok": True}


@app.post("/api/scan/auto/resume")
async def api_auto_resume() -> dict[str, Any]:
    await scanner.resume_auto()
    return {"ok": True}


@app.post("/api/scan/auto/stop")
async def api_auto_stop() -> dict[str, Any]:
    global _auto_current_code
    await scanner.stop_auto()
    if _auto_current_code is not None:
        save_settings({"current_code": _auto_current_code})
    return {"ok": True}


@app.post("/api/library/revalidate")
async def api_revalidate(body: RevalidateRequest) -> dict[str, Any]:
    codes = get_failed_codes_in_range(body.start_code, body.end_code)
    if not codes:
        return {"ok": True, "message": "No failed scans in this range to recheck.", "count": 0}

    async def run_revalidate() -> None:
        settings_save_counter = 0

        async def on_result(result: ScanResult) -> None:
            saved = await asyncio.to_thread(upsert_product, result.to_dict(), force=True)
            settings_save_counter += 1
            await broadcast("scan_result", saved)
            await broadcast("library_updated", {"code": result.code})

        try:
            await scanner.start_auto(
                start_code=body.start_code,
                end_code=body.end_code,
                skip_scanned=False,
                scanned_codes=set(),
                on_result=on_result,
                codes_override=codes,
                **_gentle_fix_scan_kwargs(body, enrich_only_codes=set()),
            )
        except Exception as exc:
            await broadcast("auto_finished", {"error": str(exc), "mode": "revalidate"})

    _track_background(run_revalidate())
    await broadcast("auto_started", {"mode": "revalidate", "count": len(codes), **body.model_dump()})
    return {"ok": True, "count": len(codes), "started": True}


@app.post("/api/library/enrich-incomplete")
async def api_enrich_incomplete(body: RevalidateRequest) -> dict[str, Any]:
    codes = get_incomplete_valid_codes(for_fix=True)
    if not codes:
        return {"ok": True, "message": "No incomplete hits to fix.", "count": 0}

    async def run_enrich() -> None:
        async def on_result(result: ScanResult) -> None:
            saved = await asyncio.to_thread(
                lambda r=result: upsert_product(r.to_dict(), force=True, merge_missing=True)
            )
            await broadcast("scan_result", saved)
            await broadcast("library_updated", {"code": result.code})

        try:
            await scanner.start_auto(
                start_code=min(codes),
                end_code=max(codes),
                skip_scanned=False,
                scanned_codes=set(),
                on_result=on_result,
                codes_override=codes,
                **_gentle_fix_scan_kwargs(body, enrich_only_codes=set(codes)),
            )
        except Exception as exc:
            await broadcast("auto_finished", {"error": str(exc), "mode": "enrich_incomplete"})

    _track_background(run_enrich())
    await broadcast("auto_started", {"mode": "enrich_incomplete", "count": len(codes), **body.model_dump()})
    return {"ok": True, "count": len(codes), "started": True}


@app.post("/api/library/fix-problems")
async def api_fix_problems(body: RevalidateRequest) -> dict[str, Any]:
    codes = get_fix_queue_codes(body.start_code, body.end_code)
    if not codes:
        return {"ok": True, "message": "Nothing to fix in that range.", "count": 0}

    failed_count = len(get_failed_codes_in_range(body.start_code, body.end_code))
    incomplete_count = len(
        [c for c in get_incomplete_valid_codes(for_fix=True) if body.start_code <= c <= body.end_code]
    )

    incomplete_in_range = {
        c
        for c in get_incomplete_valid_codes(for_fix=True)
        if body.start_code <= c <= body.end_code
    }

    async def run_fix() -> None:
        async def on_result(result: ScanResult) -> None:
            saved = await asyncio.to_thread(
                lambda r=result: upsert_product(r.to_dict(), force=True, merge_missing=True)
            )
            await broadcast("scan_result", saved)
            await broadcast("library_updated", {"code": result.code})

        try:
            await scanner.start_auto(
                start_code=min(codes),
                end_code=max(codes),
                skip_scanned=False,
                scanned_codes=set(),
                on_result=on_result,
                codes_override=codes,
                **_gentle_fix_scan_kwargs(body, enrich_only_codes=incomplete_in_range),
            )
        except Exception as exc:
            scan_debug.log("error", "fix_problems_failed", error=str(exc))
            scanner.status = ScanStatus.IDLE
            await broadcast("auto_finished", {"error": str(exc), "mode": "fix_problems"})

    _track_background(run_fix())
    await broadcast(
        "auto_started",
        {
            "mode": "fix_problems",
            "count": len(codes),
            "failed_count": failed_count,
            "incomplete_count": incomplete_count,
            **body.model_dump(),
        },
    )
    return {
        "ok": True,
        "count": len(codes),
        "failed_count": failed_count,
        "incomplete_count": incomplete_count,
        "started": True,
    }


@app.post("/api/library/fix-throttled")
async def api_fix_throttled(body: RevalidateRequest) -> dict[str, Any]:
    codes = get_throttled_codes_in_range(body.start_code, body.end_code)
    if not codes:
        return {"ok": True, "message": "Nothing throttled in that range.", "count": 0}

    reset_count = await asyncio.to_thread(reset_throttle_for_retry, codes)

    async def run_fix() -> None:
        async def on_result(result: ScanResult) -> None:
            saved = await asyncio.to_thread(
                lambda r=result: upsert_product(r.to_dict(), force=True, merge_missing=True)
            )
            await broadcast("scan_result", saved)
            await broadcast("library_updated", {"code": result.code})

        try:
            await scanner.start_auto(
                start_code=min(codes),
                end_code=max(codes),
                skip_scanned=False,
                scanned_codes=set(),
                on_result=on_result,
                codes_override=codes,
                **_gentle_fix_scan_kwargs(body, enrich_only_codes=set()),
            )
        except Exception as exc:
            scan_debug.log("error", "fix_throttled_failed", error=str(exc))
            scanner.status = ScanStatus.IDLE
            await broadcast("auto_finished", {"error": str(exc), "mode": "fix_throttled"})

    _track_background(run_fix())
    await broadcast(
        "auto_started",
        {
            "mode": "fix_throttled",
            "count": len(codes),
            "throttled_count": len(codes),
            "reset_count": reset_count,
            **body.model_dump(),
        },
    )
    return {
        "ok": True,
        "count": len(codes),
        "throttled_count": len(codes),
        "reset_count": reset_count,
        "started": True,
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    websocket_clients.add(websocket)
    try:
        await websocket.send_text(
            json.dumps({"event": "connected", "payload": await api_status()})
        )
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        websocket_clients.discard(websocket)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/data", StaticFiles(directory=str(DATA_DIR)), name="data")
