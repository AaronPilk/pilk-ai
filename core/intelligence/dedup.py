"""Dedup helpers — URL canonicalisation + content hashing.

Used by the item store on every insert to avoid storing the same
article five times when five aggregators republish it. Pure
functions; no IO, no LLM calls. Safe to call from anywhere.
"""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Tracking parameters that don't change the actual document. Stripped
# during canonicalisation so two URLs that point at the same article
# with different UTMs collapse to one row.
_TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_id",
        "utm_name",
        "fbclid",
        "gclid",
        "gclsrc",
        "dclid",
        "mc_cid",
        "mc_eid",
        "yclid",
        "msclkid",
        "ref",
        "ref_src",
        "ref_url",
        "ref_source",
        "_ga",
        "igshid",
        "feature",
        "share",
        "share_id",
    }
)


def canonical_url(url: str) -> str:
    """Return a normalised form of ``url`` suitable for dedup.

    Steps:
      - lowercases scheme + host
      - strips fragment (#anchor)
      - drops tracking query params (utm_*, gclid, fbclid, etc.)
      - removes trailing slash on path roots (but not on real paths)
      - sorts remaining query params for stable output

    Raises ``ValueError`` for empty or non-http(s) URLs."""
    if not url or not isinstance(url, str):
        raise ValueError("url must be a non-empty string")
    raw = url.strip()
    if not raw:
        raise ValueError("url must be a non-empty string")
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError(f"unsupported scheme: {parsed.scheme!r}")
    if not parsed.netloc:
        raise ValueError(f"url missing host: {url!r}")

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    # Strip default ports.
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    path = parsed.path or "/"
    # Collapse trailing slash on root only — preserve "/posts/42/"
    # vs "/posts/42" because servers may treat them as different.
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    kept_pairs = sorted(
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if k.lower() not in _TRACKING_PARAMS
    )
    query = urlencode(kept_pairs)

    return urlunparse((scheme, netloc, path, "", query, ""))


def content_hash(*, title: str, url: str, body: str = "") -> str:
    """Stable SHA-256 over (canonical_url, normalised title, body
    prefix). The body prefix bound is so small wording differences in
    feed XML (a republish that strips a footer, an aggregator that
    truncates) collapse to the same hash."""
    try:
        cu = canonical_url(url)
    except ValueError:
        # Fall back to raw URL when canonicalisation fails (e.g.
        # missing scheme). Hash is still stable per-input — it just
        # won't merge with other tracking-stripped variants.
        cu = url.strip()
    normalised_title = " ".join((title or "").split()).lower()
    body_prefix = (body or "")[:2000]
    payload = f"{cu}\n{normalised_title}\n{body_prefix}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = ["canonical_url", "content_hash"]
