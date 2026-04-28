from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from core.api import create_app


@pytest.mark.asyncio
async def test_health_ok() -> None:
    app = create_app()
    transport = ASGITransport(app=app)
    async with (
        AsyncClient(transport=transport, base_url="http://test") as client,
        app.router.lifespan_context(app),
    ):
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

        v = await client.get("/version")
        assert v.status_code == 200
        body = v.json()
        assert "version" in body
