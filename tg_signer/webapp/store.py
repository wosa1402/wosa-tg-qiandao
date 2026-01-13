from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_NAME_RE = re.compile("^[a-zA-Z0-9\u4e00-\u9fff][a-zA-Z0-9_\u4e00-\u9fff-]{0,63}$")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_name(name: str, *, label: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            f"{label}不合法，仅允许中文/字母/数字/下划线/中划线，且必须以中文/字母或数字开头（长度 1-64）"
        )
    return name


def _read_json(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
    tmp.replace(path)


@dataclass(frozen=True)
class AccountRecord:
    account_name: str
    created_at: str
    last_login_at: Optional[str] = None
    last_error: Optional[str] = None


class AccountsStore:
    def __init__(self, path: Path):
        self._path = path

    def list(self) -> list[AccountRecord]:
        data = _read_json(self._path, default={"accounts": {}})
        accounts = data.get("accounts", {})
        records: list[AccountRecord] = []
        for name, item in accounts.items():
            records.append(
                AccountRecord(
                    account_name=name,
                    created_at=item.get("created_at") or utc_now_iso(),
                    last_login_at=item.get("last_login_at"),
                    last_error=item.get("last_error"),
                )
            )
        records.sort(key=lambda r: r.account_name)
        return records

    def get(self, account_name: str) -> Optional[AccountRecord]:
        data = _read_json(self._path, default={"accounts": {}})
        item = (data.get("accounts") or {}).get(account_name)
        if not item:
            return None
        return AccountRecord(
            account_name=account_name,
            created_at=item.get("created_at") or utc_now_iso(),
            last_login_at=item.get("last_login_at"),
            last_error=item.get("last_error"),
        )

    def ensure(self, account_name: str) -> AccountRecord:
        account_name = validate_name(account_name, label="账号名")
        data = _read_json(self._path, default={"accounts": {}})
        accounts = data.setdefault("accounts", {})
        if account_name not in accounts:
            accounts[account_name] = {"created_at": utc_now_iso()}
            _write_json(self._path, data)
        return self.get(account_name)  # type: ignore[return-value]

    def mark_login_success(self, account_name: str) -> None:
        data = _read_json(self._path, default={"accounts": {}})
        accounts = data.setdefault("accounts", {})
        item = accounts.setdefault(account_name, {"created_at": utc_now_iso()})
        item["last_login_at"] = utc_now_iso()
        item["last_error"] = None
        _write_json(self._path, data)

    def mark_logout(self, account_name: str) -> None:
        data = _read_json(self._path, default={"accounts": {}})
        accounts = data.setdefault("accounts", {})
        item = accounts.setdefault(account_name, {"created_at": utc_now_iso()})
        item["last_error"] = None
        _write_json(self._path, data)

    def mark_error(self, account_name: str, error: str) -> None:
        data = _read_json(self._path, default={"accounts": {}})
        accounts = data.setdefault("accounts", {})
        item = accounts.setdefault(account_name, {"created_at": utc_now_iso()})
        item["last_error"] = str(error)[:500]
        _write_json(self._path, data)


@dataclass(frozen=True)
class TaskRecord:
    task_name: str
    account_name: str
    type: str
    enabled: bool
    created_at: str
    updated_at: str


class TasksStore:
    def __init__(self, workdir: Path):
        self._workdir = workdir
        self._tasks_dir = self._workdir / "signs"
        self._tasks_dir.mkdir(parents=True, exist_ok=True)

    def _task_dir(self, task_name: str) -> Path:
        return self._tasks_dir / task_name

    def _meta_path(self, task_name: str) -> Path:
        return self._task_dir(task_name) / "task.meta.json"

    def _config_path(self, task_name: str) -> Path:
        return self._task_dir(task_name) / "config.json"

    def list(self) -> list[TaskRecord]:
        records: list[TaskRecord] = []
        if not self._tasks_dir.exists():
            return records
        for d in self._tasks_dir.iterdir():
            if not d.is_dir():
                continue
            meta = _read_json(d / "task.meta.json", default=None) or {}
            task_name = d.name
            records.append(
                TaskRecord(
                    task_name=task_name,
                    account_name=str(meta.get("account_name") or "my_account"),
                    type=str(meta.get("type") or "signer"),
                    enabled=bool(meta.get("enabled") or False),
                    created_at=str(meta.get("created_at") or utc_now_iso()),
                    updated_at=str(meta.get("updated_at") or utc_now_iso()),
                )
            )
        records.sort(key=lambda r: r.task_name)
        return records

    def get(self, task_name: str) -> Optional[TaskRecord]:
        task_name = validate_name(task_name, label="任务名")
        d = self._task_dir(task_name)
        if not d.exists():
            return None
        meta = _read_json(self._meta_path(task_name), default=None) or {}
        return TaskRecord(
            task_name=task_name,
            account_name=str(meta.get("account_name") or "my_account"),
            type=str(meta.get("type") or "signer"),
            enabled=bool(meta.get("enabled") or False),
            created_at=str(meta.get("created_at") or utc_now_iso()),
            updated_at=str(meta.get("updated_at") or utc_now_iso()),
        )

    def ensure(
        self, task_name: str, *, account_name: str, type: str = "signer", enabled: bool = False
    ) -> TaskRecord:
        task_name = validate_name(task_name, label="任务名")
        account_name = validate_name(account_name, label="账号名")

        d = self._task_dir(task_name)
        d.mkdir(parents=True, exist_ok=True)

        meta_path = self._meta_path(task_name)
        meta = _read_json(meta_path, default=None) or {}
        now = utc_now_iso()
        meta.setdefault("created_at", now)
        meta["updated_at"] = now
        meta["task_name"] = task_name
        meta["account_name"] = account_name
        meta["type"] = type
        meta["enabled"] = bool(enabled)
        _write_json(meta_path, meta)

        cfg_path = self._config_path(task_name)
        if not cfg_path.exists():
            _write_json(
                cfg_path,
                {
                    "_version": 3,
                    "chats": [],
                    "sign_at": "0 6 * * *",
                    "random_seconds": 0,
                    "sign_interval": 1,
                },
            )

        rec = self.get(task_name)
        if not rec:
            raise RuntimeError("任务创建失败")
        return rec

    def read_config_text(self, task_name: str) -> str:
        task_name = validate_name(task_name, label="任务名")
        cfg_path = self._config_path(task_name)
        if not cfg_path.exists():
            return ""
        return cfg_path.read_text(encoding="utf-8")

    def write_config_text(self, task_name: str, config_text: str) -> None:
        task_name = validate_name(task_name, label="任务名")
        cfg_path = self._config_path(task_name)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(config_text, encoding="utf-8")

    def touch_updated_at(self, task_name: str) -> None:
        task_name = validate_name(task_name, label="任务名")
        meta_path = self._meta_path(task_name)
        meta = _read_json(meta_path, default=None) or {}
        meta["updated_at"] = utc_now_iso()
        _write_json(meta_path, meta)


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    task_name: str
    account_name: str
    mode: str
    status: str
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    pid: Optional[int] = None
    exit_code: Optional[int] = None
    error_message: Optional[str] = None


class RunsStore:
    def __init__(self, path: Path):
        self._path = path

    def list(self) -> list[RunRecord]:
        data = _read_json(self._path, default={"runs": {}})
        runs = data.get("runs", {})
        records: list[RunRecord] = []
        for run_id, item in runs.items():
            records.append(
                RunRecord(
                    run_id=run_id,
                    task_name=str(item.get("task_name") or ""),
                    account_name=str(item.get("account_name") or ""),
                    mode=str(item.get("mode") or ""),
                    status=str(item.get("status") or "queued"),
                    created_at=str(item.get("created_at") or utc_now_iso()),
                    started_at=item.get("started_at"),
                    finished_at=item.get("finished_at"),
                    pid=item.get("pid"),
                    exit_code=item.get("exit_code"),
                    error_message=item.get("error_message"),
                )
            )
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records

    def get(self, run_id: str) -> Optional[RunRecord]:
        data = _read_json(self._path, default={"runs": {}})
        item = (data.get("runs") or {}).get(run_id)
        if not item:
            return None
        return RunRecord(
            run_id=run_id,
            task_name=str(item.get("task_name") or ""),
            account_name=str(item.get("account_name") or ""),
            mode=str(item.get("mode") or ""),
            status=str(item.get("status") or "queued"),
            created_at=str(item.get("created_at") or utc_now_iso()),
            started_at=item.get("started_at"),
            finished_at=item.get("finished_at"),
            pid=item.get("pid"),
            exit_code=item.get("exit_code"),
            error_message=item.get("error_message"),
        )

    def create(self, record: RunRecord) -> None:
        data = _read_json(self._path, default={"runs": {}})
        runs = data.setdefault("runs", {})
        runs[record.run_id] = {
            "task_name": record.task_name,
            "account_name": record.account_name,
            "mode": record.mode,
            "status": record.status,
            "created_at": record.created_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "pid": record.pid,
            "exit_code": record.exit_code,
            "error_message": record.error_message,
        }
        _write_json(self._path, data)

    def update(self, run_id: str, **fields) -> None:
        data = _read_json(self._path, default={"runs": {}})
        runs = data.setdefault("runs", {})
        if run_id not in runs:
            return
        item = runs[run_id]
        for k, v in fields.items():
            item[k] = v
        _write_json(self._path, data)
