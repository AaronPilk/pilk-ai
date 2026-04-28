from __future__ import annotations

import time
from unittest.mock import patch

import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from core.api.app import create_app
from core.config import get_settings

_HS256_SECRET = "unit-test-hs256-secret-32-bytes-min"


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


@pytest.fixture
def cloud_client() -> TestClient:
    env = {
        "PILK_CLOUD": "1",
        "SUPABASE_JWT_SECRET": _HS256_SECRET,
        "SUPABASE_URL": "https://example.supabase.co",
    }
    with patch.dict("os.environ", env, clear=False):
        get_settings.cache_clear()
        app = create_app()
        with TestClient(app) as client:
            yield client
        get_settings.cache_clear()


def test_ws_rejects_missing_token_in_cloud(cloud_client: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect) as e,
        cloud_client.websocket_connect("/ws"),
    ):
        pass
    assert e.value.code == 4401


def test_ws_accepts_valid_token_in_cloud(cloud_client: TestClient) -> None:
    token = _hs256_token()
    with cloud_client.websocket_connect(f"/ws?token={token}") as ws:
        msg = ws.receive_json()
    assert msg["type"] == "system.hello"
