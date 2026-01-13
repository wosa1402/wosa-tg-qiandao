from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from cryptography.fernet import Fernet

from tg_signer.webapp.settings import WebDavSettings, WebSettings
from tg_signer.webapp.store import utc_now_iso


@dataclass(frozen=True)
class BackupStatus:
    enabled: bool
    remote_url: Optional[str] = None
    last_pull_at: Optional[str] = None
    last_push_at: Optional[str] = None
    last_error: Optional[str] = None


def _remote_file(remote_path: str) -> str:
    remote_path = (remote_path or "").strip()
    if not remote_path:
        raise ValueError("remote_path 不能为空")
    if not remote_path.startswith("/"):
        remote_path = "/" + remote_path
    if remote_path.endswith("/"):
        return remote_path + "backup.latest.tar.gz"
    return remote_path


def _join_url(base_url: str, remote_file: str) -> str:
    base_url = (base_url or "").strip().rstrip("/")
    remote_file = remote_file.strip()
    if not remote_file.startswith("/"):
        remote_file = "/" + remote_file
    return base_url + remote_file


def _derive_fernet_key(raw: str) -> bytes:
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _safe_extract(tar: tarfile.TarFile, *, target_dir: Path) -> None:
    target_dir = target_dir.resolve()
    for member in tar.getmembers():
        name = member.name
        if name.startswith("/") or name.startswith("\\"):
            raise ValueError(f"非法备份条目: {name}")
        p = (target_dir / name).resolve()
        if not str(p).startswith(str(target_dir)):
            raise ValueError(f"非法备份条目: {name}")
    tar.extractall(target_dir)


class WebDavBackupManager:
    def __init__(self, settings: WebSettings, webdav: WebDavSettings):
        self._settings = settings
        self._webdav = webdav
        self._remote_file = _remote_file(self._webdav.remote_path)
        self._remote_url = _join_url(self._webdav.url, self._remote_file)
        self._status_path = settings.data_dir / "backup.status.json"
        self._lock = asyncio.Lock()
        self._dirty = False
        self._push_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._fernet: Optional[Fernet] = None
        if self._webdav.encryption_key:
            self._fernet = Fernet(_derive_fernet_key(self._webdav.encryption_key))

    @property
    def remote_url(self) -> str:
        return self._remote_url

    def _write_status(
        self,
        *,
        last_pull_at: Optional[str] = None,
        last_push_at: Optional[str] = None,
        last_error: Optional[str] = None,
    ) -> None:
        data = {
            "enabled": True,
            "remote_url": self._remote_url,
            "last_pull_at": last_pull_at,
            "last_push_at": last_push_at,
            "last_error": last_error,
        }
        self._status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._status_path.with_suffix(self._status_path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._status_path)

    def get_status(self) -> BackupStatus:
        if not self._status_path.exists():
            return BackupStatus(enabled=True, remote_url=self._remote_url)
        data = json.loads(self._status_path.read_text(encoding="utf-8") or "{}")
        return BackupStatus(
            enabled=bool(data.get("enabled", True)),
            remote_url=data.get("remote_url") or self._remote_url,
            last_pull_at=data.get("last_pull_at"),
            last_push_at=data.get("last_push_at"),
            last_error=data.get("last_error"),
        )

    async def schedule_push(self, reason: str = "") -> None:
        self._dirty = True
        self._push_event.set()

    async def run_scheduler(self) -> None:
        interval = max(30, int(self._webdav.interval_seconds or 300))
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._push_event.wait(), timeout=interval)
                self._push_event.clear()
            except asyncio.TimeoutError:
                pass

            if self._stop_event.is_set():
                return
            if not self._dirty:
                continue
            try:
                await self.push()
                self._dirty = False
            except Exception:
                # keep dirty so we retry later
                await asyncio.sleep(5)

    def stop(self) -> None:
        self._stop_event.set()
        self._push_event.set()

    async def pull_if_exists(self) -> bool:
        async with self._lock:
            try:
                content = await self._download()
                if content is None:
                    self._write_status(last_error=None)
                    return False
                await self._restore(content)
                self._write_status(last_pull_at=utc_now_iso(), last_error=None)
                self._dirty = False
                return True
            except Exception as e:
                self._write_status(last_error=str(e)[:500])
                raise

    async def push(self) -> None:
        async with self._lock:
            try:
                content = await self._make_backup()
                await self._upload(content)
                self._write_status(last_push_at=utc_now_iso(), last_error=None)
            except Exception as e:
                self._write_status(last_error=str(e)[:500])
                raise

    async def _download(self) -> Optional[bytes]:
        async with httpx.AsyncClient(
            auth=(self._webdav.username, self._webdav.password),
            timeout=60,
            follow_redirects=True,
        ) as client:
            resp = await client.get(self._remote_url)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.content
            if self._fernet:
                data = self._fernet.decrypt(data)
            return data

    async def _upload(self, content: bytes) -> None:
        data = self._fernet.encrypt(content) if self._fernet else content
        async with httpx.AsyncClient(
            auth=(self._webdav.username, self._webdav.password),
            timeout=120,
            follow_redirects=True,
        ) as client:
            await self._ensure_remote_dirs(client)
            resp = await client.put(
                self._remote_url,
                content=data,
                headers={"Content-Type": "application/octet-stream"},
            )
            resp.raise_for_status()

    async def _ensure_remote_dirs(self, client: httpx.AsyncClient) -> None:
        remote_dir = self._remote_file.rsplit("/", 1)[0]
        if not remote_dir:
            return
        parts = [p for p in remote_dir.split("/") if p]
        base = self._webdav.url.rstrip("/")
        prefix = ""
        for p in parts:
            prefix += f"/{p}"
            url = base + prefix
            try:
                resp = await client.request("MKCOL", url)
                if resp.status_code in {201, 405, 409}:
                    continue
            except Exception:
                continue

    async def _make_backup(self) -> bytes:
        sessions_dir = self._settings.sessions_dir
        workdir = self._settings.workdir
        accounts_file = self._settings.data_dir / "accounts.json"
        runs_file = self._settings.data_dir / "runs.json"
        config_file = self._settings.data_dir / "web.config.json"

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            if sessions_dir.exists():
                tar.add(sessions_dir, arcname="sessions")
            if workdir.exists():
                tar.add(workdir, arcname="workdir")
            if accounts_file.exists():
                tar.add(accounts_file, arcname="accounts.json")
            if runs_file.exists():
                tar.add(runs_file, arcname="runs.json")
            if config_file.exists():
                tar.add(config_file, arcname="web.config.json")
        buf.seek(0)
        return buf.read()

    async def _restore(self, content: bytes) -> None:
        target = self._settings.data_dir
        sessions_dir = self._settings.sessions_dir
        workdir = self._settings.workdir
        for d in [sessions_dir, workdir]:
            if d.exists():
                shutil.rmtree(d)
        sessions_dir.mkdir(parents=True, exist_ok=True)
        workdir.mkdir(parents=True, exist_ok=True)

        buf = io.BytesIO(content)
        with tarfile.open(fileobj=buf, mode="r:gz") as tar:
            _safe_extract(tar, target_dir=target)
