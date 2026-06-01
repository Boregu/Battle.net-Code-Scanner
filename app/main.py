import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.library import (
    count_products,
    count_incomplete_valid_codes,
    delete_product,
    ensure_dirs,
    export_library,
    get_product,
    get_failed_codes_in_range,
    get_incomplete_valid_codes,
    get_scanned_codes_in_range,
    get_skip_codes_in_range,
    list_games,
    list_products,
    list_recent_products,
    load_settings,
    next_unscanned_code,
    save_settings,
    upsert_product,
)
from app.scanner import BattleNetScanner, ScanResult

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"

app = FastAPI(title="Battle.net Code Scanner")
scanner = BattleNetScanner()
websocket_clients: set[WebSocket] = set()
_auto_current_code: int | None = None
_background_tasks: set[asyncio.Task] = set()


def _track_background(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


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


class RevalidateRequest(BaseModel):
    start_code: int
    end_code: int
    delay_ms: int = Field(default=0, ge=0, le=600000)
    concurrency: int = Field(default=4, ge=1)
    region: str = "us"
    headless: bool = True


async def broadcast(event: str, payload: dict[str, Any]) -> None:
    dead: list[WebSocket] = []
    message = json.dumps({"event": event, "payload": payload})
    for ws in websocket_clients:
        try:
            await ws.send_text(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        websocket_clients.discard(ws)


async def persist_result(result: ScanResult, *, force: bool = False, notify_library: bool = True) -> dict[str, Any]:
    saved = upsert_product(result.to_dict(), force=force)
    if notify_library:
        await broadcast("library_updated", {"code": result.code})
    return saved


async def scanner_event_handler(event: str, payload: dict[str, Any]) -> None:
    await broadcast(event, payload)
    if event == "auto_finished" and _auto_current_code is not None:
        save_settings({"current_code": _auto_current_code})


@app.on_event("startup")
async def startup_event() -> None:
    ensure_dirs()
    scanner.set_event_handler(scanner_event_handler)


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await scanner.close()


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
async def browse_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "browse.html")


@app.get("/browse")
async def browse_alias() -> FileResponse:
    return FileResponse(STATIC_DIR / "browse.html")


@app.get("/scanner")
async def scanner_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/public/stats")
async def api_public_stats() -> dict[str, Any]:
    return {
        "products": count_products(valid_only=True),
        "games": list_games(valid_only=True),
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
    items = list_products(
        valid_only=True,
        search=search,
        game=game,
        sort=sort if sort in ("code", "code_desc", "game", "game_desc", "name", "checked") else "name",
        limit=safe_limit,
        offset=max(0, offset),
    )
    return {
        "items": items,
        "total": count_products(valid_only=True, search=search, game=game),
        "limit": safe_limit,
        "offset": max(0, offset),
    }


@app.get("/api/status")
async def api_status() -> dict[str, Any]:
    settings = load_settings()
    return {
        "scanner_status": scanner.status.value,
        "logged_in": scanner.is_logged_in,
        "settings": settings,
        "library_count": count_products(),
        "valid_count": count_products(valid_only=True),
        "incomplete_count": count_incomplete_valid_codes(),
        **scanner.activity_snapshot(),
    }


@app.get("/api/settings")
async def api_get_settings() -> dict[str, Any]:
    return load_settings()


@app.put("/api/settings")
async def api_update_settings(body: SettingsUpdate) -> dict[str, Any]:
    updates = body.model_dump(exclude_none=True)
    return save_settings(updates)


@app.get("/api/library/recent")
async def api_library_recent(limit: int = 5) -> list[dict[str, Any]]:
    return list_recent_products(limit=min(max(limit, 1), 20))


@app.get("/api/library-count")
async def api_library_count(valid_only: bool = False, search: str = "", game: str = "") -> dict[str, int]:
    return {"count": count_products(valid_only=valid_only, search=search, game=game)}


@app.get("/api/library")
async def api_library(
    valid_only: bool = False,
    search: str = "",
    game: str = "",
    sort: str = "code",
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    safe_limit = None
    if limit is not None:
        safe_limit = min(max(limit, 1), 500)
    return list_products(
        valid_only=valid_only,
        search=search,
        game=game,
        sort=sort,
        limit=safe_limit,
        offset=max(0, offset),
    )


@app.get("/api/library/{code}")
async def api_get_library_item(code: int) -> dict[str, Any]:
    item = get_product(code)
    if not item:
        return {"error": "not_found"}
    return item


@app.delete("/api/library/{code}")
async def api_delete_library_item(code: int) -> dict[str, Any]:
    deleted = delete_product(code)
    await broadcast("library_updated", {"code": code, "deleted": deleted})
    return {"deleted": deleted}


@app.get("/api/library/export/json")
async def api_export_library() -> list[dict[str, Any]]:
    return export_library()


@app.post("/api/login/open")
async def api_open_login() -> dict[str, Any]:
    await scanner.open_login_browser()
    return {"ok": True}


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
    result = await scanner.scan_code(body.code, region=body.region, headless=body.headless)
    saved = await persist_result(result, force=True)
    await broadcast("scan_result", saved)
    return {"result": saved}


@app.post("/api/scan/auto/start")
async def api_auto_start(body: AutoScanRequest) -> dict[str, Any]:
    global _auto_current_code
    save_settings(
        {
            "start_code": body.start_code,
            "end_code": body.end_code,
            "current_code": body.start_code,
            "delay_ms": body.delay_ms,
            "concurrency": body.concurrency,
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
        saved = await asyncio.to_thread(upsert_product, result.to_dict(), force=False)
        _auto_current_code = max(_auto_current_code or 0, result.code + 1)
        settings_save_counter += 1
        if result.valid or settings_save_counter % 8 == 0:
            save_settings({"current_code": _auto_current_code})
        await broadcast("scan_result", saved)
        if result.valid:
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
            delay_ms=body.delay_ms,
            region=body.region,
            headless=body.headless,
            concurrency=body.concurrency,
            skip_scanned=bool(skip_codes),
            scanned_codes=skip_codes,
            on_result=on_result,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    await broadcast("auto_started", body.model_dump())
    return {"ok": True, "started": True}


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
            nonlocal settings_save_counter
            saved = await asyncio.to_thread(upsert_product, result.to_dict(), force=True)
            settings_save_counter += 1
            await broadcast("scan_result", saved)
            if result.valid:
                await broadcast("library_updated", {"code": result.code})

        try:
            await scanner.start_auto(
                start_code=body.start_code,
                end_code=body.end_code,
                delay_ms=body.delay_ms,
                region=body.region,
                headless=body.headless,
                concurrency=body.concurrency,
                skip_scanned=False,
                scanned_codes=set(),
                on_result=on_result,
                codes_override=codes,
                browser_only=True,
            )
        except Exception as exc:
            await broadcast("auto_finished", {"error": str(exc), "mode": "revalidate"})

    _track_background(run_revalidate())
    await broadcast("auto_started", {"mode": "revalidate", "count": len(codes), **body.model_dump()})
    return {"ok": True, "count": len(codes), "started": True}


@app.post("/api/library/enrich-incomplete")
async def api_enrich_incomplete(body: RevalidateRequest) -> dict[str, Any]:
    codes = get_incomplete_valid_codes()
    if not codes:
        return {"ok": True, "message": "No incomplete hits to fix.", "count": 0}

    async def run_enrich() -> None:
        async def on_result(result: ScanResult) -> None:
            saved = await asyncio.to_thread(
                lambda r=result: upsert_product(r.to_dict(), force=True, merge_missing=True)
            )
            await broadcast("scan_result", saved)
            if result.valid:
                await broadcast("library_updated", {"code": result.code})

        try:
            await scanner.start_auto(
                start_code=min(codes),
                end_code=max(codes),
                delay_ms=body.delay_ms,
                region=body.region,
                headless=body.headless,
                concurrency=max(1, min(body.concurrency, 2)),
                skip_scanned=False,
                scanned_codes=set(),
                on_result=on_result,
                codes_override=codes,
                browser_only=True,
            )
        except Exception as exc:
            await broadcast("auto_finished", {"error": str(exc), "mode": "enrich_incomplete"})

    _track_background(run_enrich())
    await broadcast("auto_started", {"mode": "enrich_incomplete", "count": len(codes), **body.model_dump()})
    return {"ok": True, "count": len(codes), "started": True}


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
