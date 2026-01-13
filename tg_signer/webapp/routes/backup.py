from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from tg_signer.webapp.backup import WebDavBackupManager
from tg_signer.webapp.manager import WorkerManager
from tg_signer.webapp.security import (
    issue_csrf_token,
    redirect_to_login,
    verify_csrf_token,
)

router = APIRouter()


def _get_templates(request: Request):
    return request.app.state.templates


def _require_login(request: Request):
    if request.session.get("logged_in") is not True:
        return redirect_to_login(request)
    return None


@router.get("/backup", response_class=HTMLResponse)
async def backup_page(request: Request, ok: str = "", error: str = ""):
    redirect = _require_login(request)
    if redirect:
        return redirect
    templates = _get_templates(request)
    backup_manager: WebDavBackupManager | None = getattr(request.app.state, "backup_manager", None)
    status = backup_manager.get_status() if backup_manager else None
    return templates.TemplateResponse(
        request,
        "backup.html",
        {
            "request": request,
            "csrf_token": issue_csrf_token(request),
            "status": status,
            "ok": ok == "1",
            "error": error,
        },
    )


@router.post("/backup/push")
async def backup_push(request: Request, csrf_token: str = Form("")):
    redirect = _require_login(request)
    if redirect:
        return redirect
    verify_csrf_token(request, csrf_token)
    backup_manager: WebDavBackupManager | None = getattr(request.app.state, "backup_manager", None)
    if not backup_manager:
        return RedirectResponse(url="/backup?error=未配置WebDAV", status_code=303)
    await backup_manager.push()
    return RedirectResponse(url="/backup?ok=1", status_code=303)


@router.post("/backup/pull")
async def backup_pull(request: Request, csrf_token: str = Form("")):
    redirect = _require_login(request)
    if redirect:
        return redirect
    verify_csrf_token(request, csrf_token)
    backup_manager: WebDavBackupManager | None = getattr(request.app.state, "backup_manager", None)
    if not backup_manager:
        return RedirectResponse(url="/backup?error=未配置WebDAV", status_code=303)

    manager: WorkerManager = request.app.state.worker_manager
    if manager.has_running():
        return RedirectResponse(url="/backup?error=存在运行中的任务，无法恢复备份", status_code=303)

    ok = await backup_manager.pull_if_exists()
    if not ok:
        return RedirectResponse(url="/backup?error=远端未找到备份文件", status_code=303)
    return RedirectResponse(url="/backup?ok=1", status_code=303)

