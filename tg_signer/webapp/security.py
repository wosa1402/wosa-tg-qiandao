from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint


def _parse_basic_auth(authorization: str) -> Optional[tuple[str, str]]:
    if not authorization:
        return None
    kind, _, payload = authorization.partition(" ")
    if kind.lower() != "basic" or not payload:
        return None
    try:
        decoded = base64.b64decode(payload).decode("utf-8")
    except Exception:
        return None
    username, sep, password = decoded.partition(":")
    if not sep:
        return None
    return username, password


@dataclass
class BasicAuthCredentials:
    username: str
    password: str

    def enabled(self) -> bool:
        return bool(self.username and self.password)

    def update(self, *, username: str, password: str) -> None:
        self.username = username
        self.password = password


class BasicAuthMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        credentials: BasicAuthCredentials,
        *,
        realm: str = "tg-signer",
        exempt_paths: Optional[set[str]] = None,
    ):
        super().__init__(app)
        self._credentials = credentials
        self._realm = realm
        self._exempt_paths = exempt_paths or set()

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if request.url.path in self._exempt_paths:
            return await call_next(request)

        if not self._credentials.enabled():
            return await call_next(request)

        parsed = _parse_basic_auth(request.headers.get("Authorization", ""))
        if not parsed:
            return self._unauthorized()
        username, password = parsed
        if not (
            secrets.compare_digest(username, self._credentials.username)
            and secrets.compare_digest(password, self._credentials.password)
        ):
            return self._unauthorized()
        return await call_next(request)

    def _unauthorized(self) -> Response:
        return Response(
            status_code=401,
            headers={"WWW-Authenticate": f'Basic realm="{self._realm}", charset="UTF-8"'},
        )


def ensure_logged_in(request: Request) -> None:
    if request.session.get("logged_in") is True:
        return
    raise PermissionError("not logged in")


def login(request: Request) -> None:
    request.session["logged_in"] = True


def logout(request: Request) -> None:
    request.session.clear()


def issue_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if isinstance(token, str) and token:
        return token
    token = secrets.token_urlsafe(32)
    request.session["csrf_token"] = token
    return token


def verify_csrf_token(request: Request, token: str) -> None:
    expected = request.session.get("csrf_token")
    if not expected or not token or not secrets.compare_digest(str(expected), str(token)):
        raise PermissionError("csrf validation failed")


def redirect_to_login(request: Request, *, next_path: str | None = None) -> Response:
    next_path = next_path or request.url.path
    query = urlencode({"next": next_path})
    return RedirectResponse(url=f"/login?{query}", status_code=303)
