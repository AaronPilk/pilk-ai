"""Supabase JWT authentication middleware for cloud mode.

When PILK_CLOUD=1, every request must carry an
`Authorization: Bearer <supabase_access_token>` header. Tokens are
verified against the Supabase JWT secret (HS256). On success, the
caller's user_id, email, and role are attached to request.state.auth.

Local mode (PILK_CLOUD=0) bypasses this middleware entirely — pilkd on
127.0.0.1 trusts its local caller and keeps the pre-cloud behaviour.

Public paths that skip auth even in cloud mode:
    /health, /version — liveness + build info
    /ws/*             — WebSocket upgrade; WS auth lives in the route
                        itself via ?token= query param (browsers can't
                        attach Authorization headers on WS upgrade).
"""

from __future__ import annotations

from dataclasses import dataclass

import jwt
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from core.config import Settings
from core.logging import get_logger

log = get_logger("pilkd.auth")

_PUBLIC_PATHS = frozenset({"/health", "/version"})
_PUBLIC_PREFIXES = ("/ws",)


@dataclass
class AuthContext:
    user_id: str
    email: str | None
    role: str


def _is_public(path: str) -> bool:
    if path in _PUBLIC_PATHS:
        return True
    return any(path.startswith(p) for p in _PUBLIC_PREFIXES)


class SupabaseJWTMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, settings: Settings):
        super().__init__(app)
        self._jwt_secret = settings.supabase_jwt_secret

    async def dispatch(self, request: Request, call_next):
        if _is_public(request.url.path):
            return await call_next(request)

        if not self._jwt_secret:
            log.error("jwt_secret_missing_in_cloud_mode")
            return JSONResponse(
                {"error": "server_misconfigured"},
                status_code=500,
            )

        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "missing_bearer_token"},
                status_code=401,
            )

        token = auth_header.split(" ", 1)[1].strip()
        try:
            claims = jwt.decode(
                token,
                self._jwt_secret,
                algorithms=["HS256"],
                audience="authenticated",
            )
        except jwt.ExpiredSignatureError:
            return JSONResponse({"error": "token_expired"}, status_code=401)
        except jwt.InvalidTokenError as e:
            log.info("jwt_rejected", reason=str(e))
            return JSONResponse({"error": "invalid_token"}, status_code=401)

        user_id = claims.get("sub")
        if not user_id:
            return JSONResponse({"error": "invalid_token"}, status_code=401)

        request.state.auth = AuthContext(
            user_id=user_id,
            email=claims.get("email"),
            role=claims.get("role", "authenticated"),
        )
        return await call_next(request)


def current_auth(request: Request) -> AuthContext | None:
    """Return the authed caller for this request, if any.

    In cloud mode: always present on non-public routes (middleware would
    have rejected the request otherwise). In local mode: always None.
    Route handlers that need per-user isolation in Phase 2 should call
    this and branch on `None` (local mode) vs a real user_id.
    """
    return getattr(request.state, "auth", None)
