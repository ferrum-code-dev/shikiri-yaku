"""Expert web fetch with a grant check before every network request."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol

import httpx

if TYPE_CHECKING:
    from httpx import AsyncBaseTransport


HTTPS = "https"
LOCATION = "location"


class WebLog(Protocol):
    """The grants and requests shared with the Operator journal."""

    def granted(self, host: str) -> bool: ...
    def request(self, update_id: int, host: str) -> None: ...


class ExpertWeb:
    """Fetch only HTTPS hosts granted by the PO, including every redirect target."""

    def __init__(
        self,
        log: WebLog,
        update_id: int,
        *,
        timeout_s: float,
        max_bytes: int,
        max_redirects: int,
        transport: AsyncBaseTransport | None = None,
    ) -> None:
        self._log = log
        self._update_id = update_id
        self._timeout_s = timeout_s
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._transport = transport

    async def fetch(self, url: str) -> str:
        try:
            async with (
                asyncio.timeout(self._timeout_s),
                httpx.AsyncClient(
                    transport=self._transport, follow_redirects=False, trust_env=False
                ) as client,
            ):
                current = httpx.URL(url)
                for _ in range(self._max_redirects + 1):
                    if current.scheme != HTTPS or not current.host or current.userinfo:
                        return "Only HTTPS URLs without credentials are supported."
                    host = current.host.lower()
                    if not self._log.granted(host):
                        self._log.request(self._update_id, host)
                        return f"The Product Owner has been asked to allow {host}."
                    async with client.stream("GET", current) as response:
                        if response.is_redirect and LOCATION in response.headers:
                            current = current.join(response.headers[LOCATION])
                            continue
                        response.raise_for_status()
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(data) + len(chunk) > self._max_bytes:
                                return "The page exceeds the size limit."
                            data.extend(chunk)
                        return data.decode(response.encoding or "utf-8", errors="replace")
                return "Too many redirects."
        except httpx.HTTPError, httpx.InvalidURL, ValueError, LookupError, TimeoutError:
            return "The page could not be fetched."
