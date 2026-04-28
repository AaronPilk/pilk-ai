"""Embedding interface + default OpenAI HTTP implementation.

Why a tiny in-house implementation instead of the ``openai`` SDK:
the SDK pulls a large dependency tree we don't otherwise need for
this one endpoint, and the embeddings API is a simple JSON POST.
We use ``httpx`` (already a transitive dep) and add cost logging
through the existing ledger so embedding spend shows up alongside
LLM spend.

If/when we need a different embedder (Voyage, Cohere, local
sentence-transformers), implement the ``Embedder`` protocol and
wire it into ``make_embedder``. The rest of the vector pipeline
treats the embedder as opaque.
"""

from __future__ import annotations

from typing import Protocol

import httpx

from core.logging import get_logger

log = get_logger("pilkd.brain.vector.embedder")


# Pricing snapshot for cost logging (USD per million tokens).
# Source: OpenAI public pricing. Update when models change.
_OPENAI_EMBED_PRICING = {
    "text-embedding-3-small": 0.020 / 1_000_000,
    "text-embedding-3-large": 0.130 / 1_000_000,
    "text-embedding-ada-002": 0.100 / 1_000_000,
}

# Default dimensions per model. ``text-embedding-3-small`` ships at
# 1536 dims; we keep that default but allow callers to truncate via
# the API's ``dimensions`` parameter when storage matters.
_OPENAI_NATIVE_DIMS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}


class EmbeddingError(RuntimeError):
    """Raised when embedding generation fails — bad credentials,
    rate limits, network, malformed response. The vector store
    surfaces these cleanly to the caller (a tool or a route)
    rather than silently storing a NULL vector."""


class Embedder(Protocol):
    """Minimal embedder interface. Implementations should be safe
    to call concurrently from multiple coroutines and should batch
    where possible. The output vector dimensionality is reported
    by ``dim`` so the vector store can validate inputs."""

    @property
    def model(self) -> str: ...

    @property
    def dim(self) -> int: ...

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one float vector per input text, in the same order."""
        ...


class OpenAIEmbedder:
    """Embedder backed by the OpenAI ``/v1/embeddings`` endpoint.

    The OpenAI key is read at construction time, not from env at
    call time, so a missing key fails fast with a clear error.

    Cost logging is opt-in via ``ledger`` — the indexer passes one
    in so each batch shows up in ``cost_entries``; ad-hoc callers
    (e.g. tests) can omit it.
    """

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "text-embedding-3-small",
        dimensions: int | None = None,
        base_url: str = "https://api.openai.com/v1",
        timeout_s: float = 30.0,
    ) -> None:
        if not api_key:
            raise EmbeddingError(
                "OpenAI embedder requires OPENAI_API_KEY to be set. "
                "Configure it in Settings → API Keys or as an env var."
            )
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        # Resolved dimensionality used by the vector store. When the
        # caller asked for a truncated dimension, that's what we'll
        # actually return — otherwise we use the model's native dim.
        self._dim = dimensions or _OPENAI_NATIVE_DIMS.get(model, 1536)
        self._dimensions_param = dimensions  # only sent when set

    @property
    def model(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload: dict = {"model": self._model, "input": texts}
        if self._dimensions_param is not None:
            payload["dimensions"] = self._dimensions_param
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self._base_url}/embeddings"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise EmbeddingError(
                f"OpenAI embeddings request failed: {e}"
            ) from e
        if resp.status_code != 200:
            raise EmbeddingError(
                f"OpenAI embeddings returned {resp.status_code}: "
                f"{resp.text[:300]}"
            )
        body = resp.json()
        data = body.get("data") or []
        if len(data) != len(texts):
            raise EmbeddingError(
                f"OpenAI embeddings returned {len(data)} vectors for "
                f"{len(texts)} inputs"
            )
        # OpenAI returns vectors in ``data[i].embedding`` and tokens
        # in ``usage.total_tokens``. Sort by ``index`` to be safe.
        sorted_data = sorted(data, key=lambda d: int(d.get("index", 0)))
        vectors = [list(d["embedding"]) for d in sorted_data]
        for v in vectors:
            if len(v) != self._dim:
                raise EmbeddingError(
                    f"OpenAI embedding dim mismatch: got {len(v)}, "
                    f"expected {self._dim} (model={self._model})"
                )
        usage = body.get("usage") or {}
        total_tokens = int(usage.get("total_tokens") or 0)
        log.info(
            "openai_embeddings_ok",
            model=self._model,
            count=len(texts),
            tokens=total_tokens,
            dim=self._dim,
        )
        return vectors

    def estimated_cost_usd(self, total_tokens: int) -> float:
        """Estimate USD cost for ``total_tokens`` at this model's
        published rate. Used by the indexer for ledger entries."""
        rate = _OPENAI_EMBED_PRICING.get(self._model, 0.0)
        return rate * float(total_tokens)


def make_embedder(
    *,
    api_key: str | None,
    model: str = "text-embedding-3-small",
    dimensions: int | None = None,
) -> Embedder:
    """Factory the app uses to build the default embedder.

    Raises ``EmbeddingError`` cleanly when no key is configured —
    callers should surface that to the operator (the user spec
    explicitly says: 'If no embedding key is available, fail
    clearly')."""
    if not api_key:
        raise EmbeddingError(
            "No embedding API key configured. Set OPENAI_API_KEY "
            "(or Settings → API Keys → openai_api_key) and retry."
        )
    return OpenAIEmbedder(
        api_key=api_key, model=model, dimensions=dimensions,
    )
