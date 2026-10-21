"""Async client for the SolarEdge Monitoring API V2. Every call costs one credit."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from http import HTTPStatus
from typing import Any

from aiohttp import ClientError, ClientResponse, ClientSession

from .exceptions import (
    SolarEdgeAuthenticationError,
    SolarEdgeConnectionError,
    SolarEdgeCreditLimitError,
    SolarEdgeForbiddenError,
    SolarEdgeRateLimitError,
    SolarEdgeResponseError,
)
from .models import (
    DEVICE_TYPES,
    Alert,
    Device,
    Series,
    SiteMetadata,
    SiteOverview,
    Telemetry,
    parse_telemetry,
)

BASE_URL = "https://monitoringapi.solaredge.com/v2"
DEFAULT_TIMEOUT = 30.0

type TokenProvider = Callable[[], Awaitable[str]]


class SolarEdgeClient:
    """One OAuth-authenticated view of a site (Site Access)."""

    def __init__(
        self,
        session: ClientSession,
        token: TokenProvider,
        *,
        base_url: str = BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Use the caller's session; `token` returns a valid access token."""
        self._session = session
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def _get(self, path: str, params: Sequence[tuple[str, str]] = ()) -> Any:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {await self._token()}",
        }
        try:
            async with asyncio.timeout(self._timeout):
                async with self._session.get(
                    f"{self._base_url}{path}", params=list(params), headers=headers
                ) as response:
                    if response.status >= 400:
                        raise await _error_for(response)
                    return await response.json(content_type=None)
        except TimeoutError as err:
            raise SolarEdgeConnectionError(f"Timeout calling {path}") from err
        except ClientError as err:
            # Never include the request object: it carries the Authorization header.
            raise SolarEdgeConnectionError(f"Error calling {path}: {type(err).__name__}") from err
        except ValueError as err:
            raise SolarEdgeResponseError(200, f"Invalid JSON from {path}") from err

    async def async_get_site(self, site_id: int) -> SiteMetadata:
        """GET /sites/{id}; also proves the token can read the site."""
        body = await self._get(f"/sites/{site_id}")
        if not isinstance(body, dict) or "siteId" not in body:
            raise SolarEdgeResponseError(200, "Site metadata without siteId")
        return SiteMetadata.from_api(body)

    async def async_get_overview(self, site_id: int) -> SiteOverview:
        """GET /sites/{id}/overview; defaults to midnight today (site time) until now."""
        return SiteOverview.from_api(await self._get(f"/sites/{site_id}/overview"))

    async def async_get_power(self, site_id: int, start: datetime, end: datetime) -> Series:
        """GET /sites/{id}/power at QUARTER_HOUR; spans over 12 hours are rejected."""
        body = await self._get(
            f"/sites/{site_id}/power",
            [("resolution", "QUARTER_HOUR"), ("unit", "W"), *_window(start, end)],
        )
        return Series.from_api(body)

    async def async_get_lifetime_energy(
        self, site_id: int, start: datetime, end: datetime
    ) -> float | None:
        """Sum the YEAR buckets of GET /sites/{id}/energy (the only unlimited span), in Wh."""
        body = await self._get(
            f"/sites/{site_id}/energy",
            [("resolution", "YEAR"), ("unit", "WH"), *_window(start, end)],
        )
        return Series.from_api(body).total_watt_hours

    async def async_get_devices(self, site_id: int) -> list[Device]:
        """GET /sites/{id}/devices for inverters, meters and batteries."""
        body = await self._get(f"/sites/{site_id}/devices", [("types", t) for t in DEVICE_TYPES])
        return [
            Device.from_api(item)
            for item in (body if isinstance(body, list) else [])
            if isinstance(item, dict) and item.get("serialNumber")
        ]

    async def async_get_open_alerts(self, site_id: int) -> list[Alert]:
        """GET /sites/{id}/alerts, open alerts only."""
        body = await self._get(
            f"/sites/{site_id}/alerts", [("only-open", "true"), ("alerts-in-page", "100")]
        )
        return [
            Alert.from_api(item)
            for item in (body if isinstance(body, list) else [])
            if isinstance(item, dict) and "alertId" in item
        ]

    async def async_get_telemetry(
        self, site_id: int, kind: str, start: datetime, end: datetime
    ) -> Telemetry:
        """GET /sites/{id}/{inverters|meters|storage}/telemetry at HOUR (24 h max)."""
        body = await self._get(
            f"/sites/{site_id}/{kind}/telemetry", [("resolution", "HOUR"), *_window(start, end)]
        )
        return parse_telemetry(body, kind)


def _instant(value: datetime) -> str:
    """The API parses `from`/`to` as an Instant: UTC with a `Z` suffix."""
    if value.tzinfo is None:
        raise ValueError("Time window bounds must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _window(start: datetime, end: datetime) -> list[tuple[str, str]]:
    """`from`/`to` query parameters for a window."""
    return [("from", _instant(start)), ("to", _instant(end))]


async def _error_for(response: ClientResponse) -> SolarEdgeResponseError:
    """Map RFC 7807 problem responses to typed errors."""
    status = response.status
    try:
        body = await response.json(content_type=None)
    except ClientError, ValueError:
        body = None
    detail = ""
    if isinstance(body, dict):
        detail = str(body.get("detail") or body.get("error_description") or body.get("title") or "")
    try:
        detail = detail or HTTPStatus(status).phrase
    except ValueError:
        detail = detail or "error"
    if status == HTTPStatus.UNAUTHORIZED:
        return SolarEdgeAuthenticationError(status, detail)
    if status == HTTPStatus.FORBIDDEN:
        return SolarEdgeForbiddenError(status, detail)
    if status == HTTPStatus.TOO_MANY_REQUESTS:
        remaining = (response.headers.get("x-ratelimit-remaining-minute") or "").strip()
        if remaining not in ("", "0"):
            return SolarEdgeCreditLimitError(status, detail)
        retry_after = response.headers.get("retry-after", "")
        return SolarEdgeRateLimitError(
            status, detail, int(retry_after) if retry_after.isdigit() else None
        )
    return SolarEdgeResponseError(status, detail)
