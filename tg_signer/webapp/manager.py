from __future__ import annotations

import asyncio
import os
import signal
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from tg_signer.webapp.settings import WebSettings
from tg_signer.webapp.store import (
    RunRecord,
    RunsStore,
    TasksStore,
    utc_now_iso,
    validate_name,
)


class BackupScheduler(Protocol):
    async def schedule_push(self, reason: str = "") -> None: ...


@dataclass(frozen=True)
class StartRunRequest:
    task_name: str
    account_name: str
    mode: str  # run | run_once
    num_of_dialogs: int = 50


class WorkerManager:
    def __init__(
        self,
        settings: WebSettings,
        *,
        tasks_store: TasksStore,
        runs_store: RunsStore,
    ):
        self._settings = settings
        self._tasks_store = tasks_store
        self._runs_store = runs_store
        self._lock = asyncio.Lock()
        self._running_by_account: dict[str, str] = {}
        self._proc_by_run_id: dict[str, asyncio.subprocess.Process] = {}
        self._backup_scheduler: BackupScheduler | None = None

    def set_backup_scheduler(self, backup_scheduler: BackupScheduler | None) -> None:
        self._backup_scheduler = backup_scheduler

    async def _schedule_backup_push(self, reason: str) -> None:
        scheduler = self._backup_scheduler
        if not scheduler:
            return
        try:
            await scheduler.schedule_push(reason)
        except Exception:
            return

    def _run_dir(self, run_id: str) -> Path:
        return (self._settings.runs_dir / run_id).resolve()

    def _run_log(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "run.log"

    def _is_account_logged_in(self, account_name: str) -> bool:
        sessions_dir = self._settings.sessions_dir
        candidates = [
            sessions_dir / f"{account_name}.session_string",
            sessions_dir / f"{account_name}.session",
        ]
        return any(p.exists() for p in candidates)

    def get_log_path(self, run_id: str) -> Path:
        return self._run_log(run_id)

    def has_running(self) -> bool:
        return bool(self._proc_by_run_id)

    async def start(self, req: StartRunRequest) -> str:
        task_name = validate_name(req.task_name, label="任务名")
        account_name = validate_name(req.account_name, label="账号名")

        task = self._tasks_store.get(task_name)
        if not task:
            raise ValueError("任务不存在")
        if task.account_name != account_name:
            raise ValueError("任务绑定的账号与请求账号不一致")
        if not self._is_account_logged_in(account_name):
            raise RuntimeError("账号未登录，请先在 /accounts 完成登录")
        if req.mode not in {"run", "run_once"}:
            raise ValueError("mode 不合法")

        run_id: str
        async with self._lock:
            if account_name in self._running_by_account:
                raise RuntimeError("该账号已有运行中的任务，请先停止后再试")

            run_id = str(uuid.uuid4())
            run_dir = self._run_dir(run_id)
            run_dir.mkdir(parents=True, exist_ok=True)

            record = RunRecord(
                run_id=run_id,
                task_name=task_name,
                account_name=account_name,
                mode=req.mode,
                status="queued",
                created_at=utc_now_iso(),
            )
            self._runs_store.create(record)

            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["TG_SIGNER_DATA_DIR"] = str(self._settings.data_dir)

            cmd = [
                sys.executable,
                "-m",
                "tg_signer.webapp.worker",
                "--run-id",
                run_id,
                "--task-name",
                task_name,
                "--account-name",
                account_name,
                "--workdir",
                str(self._settings.workdir),
                "--session-dir",
                str(self._settings.sessions_dir),
                "--runs-dir",
                str(self._settings.runs_dir),
                "--mode",
                req.mode,
                "--num-of-dialogs",
                str(req.num_of_dialogs),
            ]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            self._proc_by_run_id[run_id] = proc
            self._running_by_account[account_name] = run_id
            self._runs_store.update(
                run_id,
                status="running",
                started_at=utc_now_iso(),
                pid=proc.pid,
            )
            asyncio.create_task(self._watch(run_id, account_name, proc))
        await self._schedule_backup_push("run_start")
        return run_id

    async def stop(self, run_id: str) -> bool:
        async with self._lock:
            proc = self._proc_by_run_id.get(run_id)
        if not proc:
            return False

        try:
            proc.send_signal(signal.SIGINT)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                return False

        async with self._lock:
            if run_id in self._proc_by_run_id:
                self._runs_store.update(run_id, status="stopping")

        asyncio.create_task(self._escalate_stop(proc))
        await self._schedule_backup_push("run_stop")
        return True

    async def _escalate_stop(
        self,
        proc: asyncio.subprocess.Process,
        *,
        sigint_timeout_seconds: int = 8,
        sigterm_timeout_seconds: int = 10,
        sigkill_timeout_seconds: int = 5,
    ) -> None:
        if proc.returncode is not None:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=sigint_timeout_seconds)
            return
        except asyncio.TimeoutError:
            pass

        try:
            proc.terminate()
        except Exception:
            return

        try:
            await asyncio.wait_for(proc.wait(), timeout=sigterm_timeout_seconds)
            return
        except asyncio.TimeoutError:
            pass

        try:
            proc.kill()
        except Exception:
            return

        try:
            await asyncio.wait_for(proc.wait(), timeout=sigkill_timeout_seconds)
        except asyncio.TimeoutError:
            return

    async def _watch(self, run_id: str, account_name: str, proc: asyncio.subprocess.Process):
        try:
            exit_code = await proc.wait()
        except Exception as e:
            async with self._lock:
                self._runs_store.update(
                    run_id,
                    status="failed",
                    finished_at=utc_now_iso(),
                    error_message=str(e)[:500],
                )
                self._proc_by_run_id.pop(run_id, None)
                self._running_by_account.pop(account_name, None)
            await self._schedule_backup_push("run_finish")
            return

        status = "success" if exit_code == 0 else "failed"
        if exit_code in {130, 143}:
            status = "stopped"

        async with self._lock:
            self._runs_store.update(
                run_id,
                status=status,
                finished_at=utc_now_iso(),
                exit_code=exit_code,
            )
            self._proc_by_run_id.pop(run_id, None)
            self._running_by_account.pop(account_name, None)
        await self._schedule_backup_push("run_finish")
