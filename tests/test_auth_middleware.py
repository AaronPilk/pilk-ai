"""Verify SupabaseJWTMiddleware accepts both HS256 (legacy secret) and
ES256 (JWKS-based) tokens — the dual-path needed during Supabase's
2025 signing-key migration.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.api.auth import SupabaseJWTMiddleware
from core.config import Settings

_HS256_SECRET = "unit-test-hs256-secret"
_SUPABASE_URL = "https://example.supabase.co"


def _app_with_middleware(settings: Settings) -> FastAPI:
    app = FastAPI()
    app.add_middleware(SupabaseJWTMiddleware, settings=settings)

    @app.get("/protected")
    async def protected():
        return {"ok": True}

    @app.get("/system/status")
    async def status():
        return {"ok": True}

    return app


def _hs256_token(**claims) -> str:
    payload = {
        "sub": "user-123",
        "email": "a@b.co",
        "role": "authenticated",
        "aud": "authenticated",
        "exp": int(time.time()) + 3600,
        **claims,
    }
    return jwt.encode(payload, _HS256_SECRET, algorithm="HS256")


def _es256_keypair() -> tuple[ec.EllipticCurvePrivateKey, dict]:
    priv = ec.generate_private_key(ec.SECP256R1())
    pub_jwk_str = jwt.algorithms.ECAlgorithm.to_jwk(priv.public_key())
    import json as _json

    jwk = _json.loads(pub_jwk_str)
    jwk["alg"] = "ES256"
    jwk["use"] = "sig"
    jwk["kid"] = "test-key-1"
    return priv, jwk


def _es256_token(priv: ec.EllipticCurvePrivateKey, kid: str) -> str:
    payload = {
        "sub": "user-es",
        "email": "es@b.co",
        "role": "authenticated",
        "aud": "authenticated",
        "exp": int(time.time()) + 3600,
    }
    return jwt.encode(
        payload, priv, algorithm="ES256", headers={"kid": kid}
    )


def _settings(**overrides) -> Settings:
    env = {
        "PILK_CLOUD": "1",
        "SUPABASE_JWT_SECRET": _HS256_SECRET,
        "SUPABASE_URL": _SUPABASE_URL,
    }
    _UNSET = object()
    secret_override = overrides.pop("supabase_jwt_secret", _UNSET)
    url_override = overrides.pop("supabase_url", _UNSET)
    if secret_override is None:
        env.pop("SUPABASE_JWT_SECRET")
    elif secret_override is not _UNSET:
        env["SUPABASE_JWT_SECRET"] = secret_override
    if url_override is None:
        env.pop("SUPABASE_URL")
    elif url_override is not _UNSET:
        env["SUPABASE_URL"] = url_override
    with patch.dict("os.environ", env, clear=False):
        return Settings(**overrides)


def test_hs256_token_accepted():
    client = TestClient(_app_with_middleware(_settings()))
    r = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {_hs256_token()}"},
    )
    assert r.status_code == 200


def test_es256_token_accepted_via_jwks():
    priv, jwk = _es256_keypair()
    token = _es256_token(priv, jwk["kid"])

    from jwt import PyJWK

    with patch(
        "jwt.PyJWKClient.get_signing_key_from_jwt",
        return_value=PyJWK(jwk),
    ):
        client = TestClient(_app_with_middleware(_settings()))
        r = client.get(
            "/protected",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert r.status_code == 200


def test_missing_bearer_rejected():
    client = TestClient(_app_with_middleware(_settings()))
    r = client.get("/protected")
    assert r.status_code == 401


def test_public_path_skips_auth():
    client = TestClient(_app_with_middleware(_settings()))
    r = client.get("/system/status")
    assert r.status_code == 200


def test_expired_hs256_returns_401():
    client = TestClient(_app_with_middleware(_settings()))
    expired = _hs256_token(exp=int(time.time()) - 60)
    r = client.get(
        "/protected", headers={"Authorization": f"Bearer {expired}"}
    )
    assert r.status_code == 401


def test_wrong_secret_hs256_rejected():
    client = TestClient(
        _app_with_middleware(_settings(supabase_jwt_secret="different-secret"))
    )
    r = client.get(
        "/protected",
        headers={"Authorization": f"Bearer {_hs256_token()}"},
    )
    assert r.status_code == 401


def test_unsupported_alg_rejected():
    # Craft an HS384 token — our middleware only accepts HS256/ES256/RS256.
    token = jwt.encode(
        {"sub": "x", "aud": "authenticated", "exp": int(time.time()) + 60},
        _HS256_SECRET,
        algorithm="HS384",
    )
    client = TestClient(_app_with_middleware(_settings()))
    r = client.get(
        "/protected", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 401


def test_es256_without_supabase_url_returns_500():
    priv, jwk = _es256_keypair()
    token = _es256_token(priv, jwk["kid"])
    client = TestClient(
        _app_with_middleware(_settings(supabase_url=None))
    )
    r = client.get(
        "/protected", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 500
