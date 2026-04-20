"""Hunter.io client — thin wrapper for email discovery.

Two endpoints, nothing else:

    domain-search    given a domain, return emails found + pattern
    email-finder     given a domain + name (first, last), guess an email

Both return ``confidence`` and a deliverability hint so the caller can
decide whether to actually send. Missing / bad keys surface as
:class:`HunterError` with the upstream message.

Docs: https://hunter.io/api-documentation/v2
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from core.logging import get_logger

log = get_logger("pilkd.hunter")

HUNTER_API_BASE = "https://api.hunter.io/v2"
DEFAULT_TIMEOUT_S = 15.0


class HunterError(Exception):
    def __init__(self, status: int, message: str, raw: Any = None):
        super().__init__(f"Hunter.io {status}: {message}")
        self.status = status
        self.message = message
        self.raw = raw


@dataclass(frozen=True)
class HunterConfig:
    api_key: str
    api_base: str = HUNTER_API_BASE


class HunterClient:
    def __init__(self, config: HunterConfig, *, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._cfg = config
        self._timeout = timeout

    def _url(self, path: str) -> str:
        return f"{self._cfg.api_base}/{path.lstrip('/')}"

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        params_full = {**params, "api_key": self._cfg.api_key}
        async with httpx.AsyncClient(timeout=self._timeout) as c:
            r = await c.get(self._url(path), params=params_full)
        if not r.is_success:
            try:
                body = r.json()
                errs = body.get("errors") or []
                message = str(
                    (errs[0] if errs else {}).get("details")
                    or r.text[:200]
                )
            except ValueError:
                message = r.text[:200] or f"HTTP {r.status_code}"
                body = None
            raise HunterError(status=r.status_code, message=message, raw=body)
        try:
            return r.json()
        except ValueError as e:
            raise HunterError(
                status=r.status_code, message="Hunter.io returned non-JSON"
            ) from e

    async def domain_search(
        self, domain: str, *, limit: int = 10
    ) -> dict[str, Any]:
        return await self._get(
            "domain-search",
            {"domain": domain.strip(), "limit": int(limit)},
        )

    async def email_finder(
        self,
        domain: str,
        *,
        first_name: str | None = None,
        last_name: str | None = None,
        full_name: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"domain": domain.strip()}
        if full_name:
            params["full_name"] = full_name.strip()
        if first_name:
            params["first_name"] = first_name.strip()
        if last_name:
            params["last_name"] = last_name.strip()
        return await self._get("email-finder", params)


__all__ = ["HunterClient", "HunterConfig", "HunterError"]
