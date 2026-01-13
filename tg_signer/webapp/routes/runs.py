from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from tg_signer.webapp.manager import WorkerManager
from tg_signer.webapp.security import (
    issue_csrf_token,
    redirect_to_login,
    verify_csrf_token,
)
from tg_signer.webapp.store import RunsStore

router = APIRouter()


def _get_templates(request: Request):
    return request.app.state.templates


def _require_login(request: Request):
    if request.session.get("logged_in") is not True:
        return redirect_to_login(request)
    return None


@router.get("/runs", response_class=HTMLResponse)
async def runs_page(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    templates = _get_templates(request)
    runs_store: RunsStore = request.app.state.runs_store
    items = [r.__dict__ for r in runs_store.list()]
    return templates.TemplateResponse(
        request,
        "runs.html",
        {"request": request, "items": items, "csrf_token": issue_csrf_token(request)},
    )


@router.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail_page(request: Request, run_id: str):
    redirect = _require_login(request)
    if redirect:
        return redirect
    templates = _get_templates(request)
    runs_store: RunsStore = request.app.state.runs_store
    run = runs_store.get(run_id)
    if not run:
        return RedirectResponse(url="/runs", status_code=303)
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {"request": request, "run": run, "csrf_token": issue_csrf_token(request)},
    )


@router.post("/runs/{run_id}/stop")
async def stop_run(request: Request, run_id: str, csrf_token: str = Form("")):
    redirect = _require_login(request)
    if redirect:
        return redirect
    verify_csrf_token(request, csrf_token)
    manager: WorkerManager = request.app.state.worker_manager
    await manager.stop(run_id)
    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


async def _tail_file(
    request: Request,
    path: Path,
    *,
    ping_interval_seconds: int = 15,
):
    offset = 0
    ticks = 0
    while not path.exists():
        if await request.is_disconnected():
            return
        yield "event: log\ndata: (等待日志文件创建...)\n\n"
        await asyncio.sleep(1)

    while True:
        if await request.is_disconnected():
            return
        try:
            with path.open("rb") as fp:
                fp.seek(offset)
                chunk = fp.read()
                if chunk:
                    offset += len(chunk)
                    ticks = 0
                    text = chunk.decode("utf-8", errors="replace")
                    for line in text.splitlines():
                        safe = line.replace("\r", "")
                        yield f"event: log\ndata: {safe}\n\n"
        except FileNotFoundError:
            pass

        await asyncio.sleep(1)
        ticks += 1
        if ticks >= ping_interval_seconds:
            ticks = 0
            yield "event: ping\ndata: ping\n\n"


@router.get("/runs/{run_id}/logs/stream")
async def stream_run_logs(request: Request, run_id: str):
    redirect = _require_login(request)
    if redirect:
        return redirect
    manager: WorkerManager = request.app.state.worker_manager
    log_path = manager.get_log_path(run_id)
    return StreamingResponse(
        _tail_file(request, log_path),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
