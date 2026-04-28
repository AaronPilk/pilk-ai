"""Supabase JWT authentication middleware for cloud mode.

When PILK_CLOUD=1, every request must carry an
`Authorization: Bearer <supabase_access_token>` header. Tokens are
verified against whatever signing material Supabase currently uses:

    • HS256 — legacy symmetric secret (SUPABASE_JWT_SECRET).
    • ES256 / RS256 — asymmetric signing keys fetched from the project's
      JWKS endpoint at `{SUPABASE_URL}/auth/v1/.well-known/jwks.json`.

The middleware picks the path based on the token header's `alg`, so a
single deployment works before, during, and after Supabase's 2025
migration to asymmetric signing keys without a config change.

On success, the caller's user_id, email, and role are attached to
request.state.auth.

Local mode (PILK_CLOUD=0) bypasses this middleware entirely — pilkd on
127.0.0.1 trusts its local caller and keeps the pre-cloud behaviour.

Public paths that skip auth even in cloud mode:
    /health, /version — bare liveness/version checks only.

WebSocket auth is handled in the WS route via `?token=` because browsers
can't attach Authorization headers on upgrade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import jwt
from jwt import PyJWKClient
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from core.config import Settings
from core.logging import get_logger

log = get_logger("pilkd.auth")

_PUBLIC_PATHS = frozenset({"/health", "/version"})

_ASYMMETRIC_ALGS = ("ES256", "RS256")
_HS256_MIN_SECRET_BYTES = 32


@dataclass
class AuthContext:
    user_id: str
    email: str | None
    role: str


def _is_public(path: str) -> bool:
    return path in _PUBLIC_PATHS


def _decode_claims(
    *,
    token: str,
    jwt_secret: str | None,
    jwks_client: PyJWKClient | None,
) -> dict[str, Any]:
    """Decode + verify a Supabase access token.

    Shared by HTTP middleware and WS auth so cloud-mode verification
    stays identical across transports.
    """
    unverified = jwt.get_unverified_header(token)
    alg = unverified.get("alg", "")
    if alg in _ASYMMETRIC_ALGS:
        if jwks_client is None:
            raise RuntimeError("jwks_client_unconfigured")
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=[alg],
            audience="authenticated",
        )
    if alg == "HS256":
        if not jwt_secret:
            raise RuntimeError("jwt_secret_missing_in_cloud_mode")
        if len(jwt_secret.encode("utf-8")) < _HS256_MIN_SECRET_BYTES:
            raise RuntimeError("jwt_secret_too_short_for_hs256")
        return jwt.decode(
            token,
            jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
        )
    raise jwt.InvalidTokenError(f"unsupported algorithm: {alg}")


def decode_auth_context(
    *,
    token: str,
    jwt_secret: str | None,
    jwks_client: PyJWKClient | None,
) -> AuthContext:
    """Decode a token into AuthContext or raise."""
    claims = _decode_claims(
        token=token,
        jwt_secret=jwt_secret,
        jwks_client=jwks_client,
    )
    user_id = claims.get("sub")
    if not user_id:
        raise jwt.InvalidTokenError("missing sub claim")
    return AuthContext(
        user_id=user_id,
        email=claims.get("email"),
        role=claims.get("role", "authenticated"),
    )


class SupabaseJWTMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, settings: Settings):
        super().__init__(app)
        self._jwt_secret = settings.supabase_jwt_secret
        self._jwks_client: PyJWKClient | None = None
        if settings.supabase_url:
            jwks_url = (
                settings.supabase_url.rstrip("/")
                + "/auth/v1/.well-known/jwks.json"
            )
            # PyJWKClient caches keys in-memory and only refetches on
            # unknown-kid. 1-hour lifespan covers Supabase's typical
            # rotation cadence without hammering the JWKS endpoint.
            self._jwks_client = PyJWKClient(
                jwks_url, cache_keys=True, lifespan=3600
            )

    async def dispatch(self, request: Request, call_next):
        if _is_public(request.url.path):
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if not auth_header.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "missing_bearer_token"},
                status_code=401,
            )

        token = auth_header.split(" ", 1)[1].strip()

        try:
            auth = decode_auth_context(
                token=token,
                jwt_secret=self._jwt_secret,
                jwks_client=self._jwks_client,
            )
        except RuntimeError as e:
            log.error(str(e))
            return JSONResponse(
                {"error": "server_misconfigured"},
                status_code=500,
            )
        except jwt.ExpiredSignatureError:
            return JSONResponse({"error": "token_expired"}, status_code=401)
        except jwt.InvalidTokenError as e:
            log.info("jwt_rejected", reason=str(e))
            return JSONResponse({"error": "invalid_token"}, status_code=401)
        request.state.auth = auth
        return await call_next(request)


def current_auth(request: Request) -> AuthContext | None:
    """Return the authed caller for this request, if any.

    In cloud mode: always present on non-public routes (middleware would
    have rejected the request otherwise). In local mode: always None.
    Route handlers that need per-user isolation in Phase 2 should call
    this and branch on `None` (local mode) vs a real user_id.
    """
    return getattr(request.state, "auth", None)
