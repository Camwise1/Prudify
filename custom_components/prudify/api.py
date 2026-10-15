"""A small client for Prudify's HTTP API."""

from __future__ import annotations

from typing import Any

import aiohttp

_TIMEOUT = aiohttp.ClientTimeout(total=15)


class PrudifyError(Exception):
    """Prudify could not be reached or did not answer sensibly."""


class PrudifyAuthError(PrudifyError):
    """The API key was refused."""


class PrudifyClient:
    def __init__(self, session: aiohttp.ClientSession, url: str, api_key: str) -> None:
        self._session = session
        self.url = url.rstrip("/")
        self._headers = {"X-Api-Key": api_key, "Accept": "application/json"}

    async def _call(self, method: str, path: str) -> Any:
        try:
            async with self._session.request(
                method, f"{self.url}/api/v1{path}", headers=self._headers, timeout=_TIMEOUT
            ) as response:
                if response.status in (401, 403):
                    raise PrudifyAuthError("The API key was not accepted")
                if response.status >= 400:
                    raise PrudifyError(f"Prudify answered HTTP {response.status} for {path}")
                return await response.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError) as exc:
            raise PrudifyError(f"Could not reach Prudify at {self.url}: {exc}") from exc

    async def summary(self) -> dict[str, Any]:
        return await self._call("GET", "/system/summary")

    async def pause(self) -> None:
        await self._call("POST", "/queue/pause")

    async def resume(self) -> None:
        await self._call("POST", "/queue/resume")

    async def scan(self) -> None:
        await self._call("POST", "/books/scan")
