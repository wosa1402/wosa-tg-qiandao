from __future__ import annotations

import json
import shutil
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from tg_signer.config import SignConfigV3
from tg_signer.webapp.manager import StartRunRequest, WorkerManager
from tg_signer.webapp.security import (
    issue_csrf_token,
    redirect_to_login,
    verify_csrf_token,
)
from tg_signer.webapp.store import AccountsStore, TasksStore, validate_name

router = APIRouter()


def _get_templates(request: Request):
    return request.app.state.templates


def _require_login(request: Request):
    if request.session.get("logged_in") is not True:
        return redirect_to_login(request)
    return None


def _quote_segment(value: str) -> str:
    return quote(value, safe="")


def _validate_signer_config(raw: Any) -> dict[str, Any]:
    loaded = SignConfigV3.load(raw)
    if not loaded:
        raise ValueError("配置不合法：无法匹配当前/旧版本配置结构")
    config, _from_old = loaded
    return config.to_jsonable()


@router.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request):
    redirect = _require_login(request)
    if redirect:
        return redirect
    templates = _get_templates(request)
    accounts_store: AccountsStore = request.app.state.accounts_store
    tasks_store: TasksStore = request.app.state.tasks_store

    accounts = [a.account_name for a in accounts_store.list()]
    tasks = [t.__dict__ for t in tasks_store.list()]
    return templates.TemplateResponse(
        request,
        "tasks.html",
        {
            "request": request,
            "accounts": accounts,
            "tasks": tasks,
            "csrf_token": issue_csrf_token(request),
            "error": None,
            "form": None,
        },
    )


@router.post("/tasks")
async def create_task(
    request: Request,
    task_name: str = Form(""),
    account_name: str = Form(""),
    csrf_token: str = Form(""),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    verify_csrf_token(request, csrf_token)
    templates = _get_templates(request)
    accounts_store: AccountsStore = request.app.state.accounts_store
    tasks_store: TasksStore = request.app.state.tasks_store
    try:
        task_name = validate_name(task_name, label="任务名")
        account_name = validate_name(account_name, label="账号名")
    except ValueError as e:
        accounts = [a.account_name for a in accounts_store.list()]
        tasks = [t.__dict__ for t in tasks_store.list()]
        return templates.TemplateResponse(
            request,
            "tasks.html",
            {
                "request": request,
                "accounts": accounts,
                "tasks": tasks,
                "csrf_token": issue_csrf_token(request),
                "error": str(e),
                "form": {"task_name": task_name, "account_name": account_name},
            },
            status_code=400,
        )

    tasks_store.ensure(task_name, account_name=account_name, type="signer", enabled=False)
    backup_manager = getattr(request.app.state, "backup_manager", None)
    if backup_manager:
        await backup_manager.schedule_push("task_create")
    return RedirectResponse(url="/tasks", status_code=303)


@router.get("/tasks/{task_name}/edit", response_class=HTMLResponse)
async def edit_task_page(request: Request, task_name: str, ok: str = ""):
    redirect = _require_login(request)
    if redirect:
        return redirect
    templates = _get_templates(request)
    tasks_store: TasksStore = request.app.state.tasks_store
    task_name = validate_name(task_name, label="任务名")
    task = tasks_store.get(task_name)
    if not task:
        return RedirectResponse(url="/tasks", status_code=303)
    config_text = tasks_store.read_config_text(task_name)
    return templates.TemplateResponse(
        request,
        "task_edit.html",
        {
            "request": request,
            "task": task,
            "config_text": config_text,
            "csrf_token": issue_csrf_token(request),
            "ok": ok == "1",
        },
    )


@router.post("/tasks/{task_name}/edit", response_class=HTMLResponse)
async def edit_task_save(
    request: Request,
    task_name: str,
    config_text: str = Form(""),
    csrf_token: str = Form(""),
):
    redirect = _require_login(request)
    if redirect:
        return redirect
    verify_csrf_token(request, csrf_token)
    templates = _get_templates(request)
    tasks_store: TasksStore = request.app.state.tasks_store
    task_name = validate_name(task_name, label="任务名")
    task = tasks_store.get(task_name)
    if not task:
        return RedirectResponse(url="/tasks", status_code=303)

    try:
        raw = json.loads(config_text or "{}")
        validated = _validate_signer_config(raw)
        new_text = json.dumps(validated, ensure_ascii=False, indent=2)
        tasks_store.write_config_text(task_name, new_text + "\n")
        tasks_store.touch_updated_at(task_name)
        backup_manager = getattr(request.app.state, "backup_manager", None)
        if backup_manager:
            await backup_manager.schedule_push("task_save")
    except Exception as e:
        return templates.TemplateResponse(
            request,
            "task_edit.html",
            {
                "request": request,
                "task": task,
                "config_text": config_text,
                "csrf_token": issue_csrf_token(request),
                "error": str(e),
                "ok": False,
            },
            status_code=400,
        )

    return RedirectResponse(
        url=f"/tasks/{_quote_segment(task_name)}/edit?ok=1", status_code=303
    )


@router.post("/tasks/{task_name}/delete")
async def delete_task(request: Request, task_name: str, csrf_token: str = Form("")):
    redirect = _require_login(request)
    if redirect:
        return redirect
    verify_csrf_token(request, csrf_token)

    tasks_store: TasksStore = request.app.state.tasks_store
    task_name = validate_name(task_name, label="任务名")
    task = tasks_store.get(task_name)
    if task:
        task_dir = tasks_store._task_dir(task_name)  # noqa: SLF001
        if task_dir.exists():
            shutil.rmtree(task_dir)
        backup_manager = getattr(request.app.state, "backup_manager", None)
        if backup_manager:
            await backup_manager.schedule_push("task_delete")
    return RedirectResponse(url="/tasks", status_code=303)


@router.post("/tasks/{task_name}/run-once")
async def run_once_task(request: Request, task_name: str, csrf_token: str = Form("")):
    redirect = _require_login(request)
    if redirect:
        return redirect
    verify_csrf_token(request, csrf_token)
    tasks_store: TasksStore = request.app.state.tasks_store
    manager: WorkerManager = request.app.state.worker_manager

    task_name = validate_name(task_name, label="任务名")
    task = tasks_store.get(task_name)
    if not task:
        return RedirectResponse(url="/tasks", status_code=303)

    run_id = await manager.start(
        StartRunRequest(
            task_name=task.task_name,
            account_name=task.account_name,
            mode="run_once",
        )
    )
    backup_manager = getattr(request.app.state, "backup_manager", None)
    if backup_manager:
        await backup_manager.schedule_push("run_start")
    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


@router.post("/tasks/{task_name}/run")
async def run_task(request: Request, task_name: str, csrf_token: str = Form("")):
    redirect = _require_login(request)
    if redirect:
        return redirect
    verify_csrf_token(request, csrf_token)
    tasks_store: TasksStore = request.app.state.tasks_store
    manager: WorkerManager = request.app.state.worker_manager

    task_name = validate_name(task_name, label="任务名")
    task = tasks_store.get(task_name)
    if not task:
        return RedirectResponse(url="/tasks", status_code=303)

    run_id = await manager.start(
        StartRunRequest(
            task_name=task.task_name,
            account_name=task.account_name,
            mode="run",
        )
    )
    backup_manager = getattr(request.app.state, "backup_manager", None)
    if backup_manager:
        await backup_manager.schedule_push("run_start")
    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)
