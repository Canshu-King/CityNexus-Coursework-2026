"""Optional SearXNG sourceful search adapter.

SearXNG is deliberately not a required runtime dependency. When configured,
it supplies URL-bearing evidence before the deterministic layer consumes it;
when absent, the native DeepSeek web-search path remains available.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx


@dataclass(frozen=True)
class SearXNGObservation:
    query: str
    url: str
    title: str
    domain: str
    content: str
    captured_at: str
    source_class: str = "OPEN_WEB"
    provider_key: str = "searxng"


class SearXNGSearchProvider:
    def __init__(self, base_url: str, *, client: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=10.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def search(self, query: str, *, limit: int = 5) -> tuple[SearXNGObservation, ...]:
        response = self._client.get(
            f"{self.base_url}/search",
            params={"q": query, "format": "json", "language": "en"},
        )
        response.raise_for_status()
        payload = response.json()
        captured_at = datetime.now(timezone.utc).isoformat()
        observations: list[SearXNGObservation] = []
        for item in (payload.get("results") or [])[:limit]:
            url = str(item.get("url") or "")
            if not url:
                continue
            observations.append(
                SearXNGObservation(
                    query=query,
                    url=url,
                    title=str(item.get("title") or urlparse(url).netloc),
                    domain=urlparse(url).netloc,
                    content=str(item.get("content") or "")[:2000],
                    captured_at=captured_at,
                )
            )
        return tuple(observations)
