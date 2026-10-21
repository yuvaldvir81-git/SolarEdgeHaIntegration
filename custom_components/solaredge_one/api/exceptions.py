"""Errors raised by the SolarEdge Monitoring API V2 client."""

from __future__ import annotations


class SolarEdgeError(Exception):
    """Base error for the cloud API."""


class SolarEdgeConnectionError(SolarEdgeError):
    """Timeout or transport failure."""


class SolarEdgeResponseError(SolarEdgeError):
    """The API answered with an error status or an unusable body."""

    def __init__(self, status: int, detail: str) -> None:
        """Keep the status; the message never contains credentials or URLs with secrets."""
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class SolarEdgeAuthenticationError(SolarEdgeResponseError):
    """401: the token is missing, expired or revoked."""


class SolarEdgeForbiddenError(SolarEdgeResponseError):
    """403: no access to the site, a missing scope, or a tier gate."""


class SolarEdgeRateLimitError(SolarEdgeResponseError):
    """429 from the per-minute limit; retry after `retry_after` seconds."""

    def __init__(self, status: int, detail: str, retry_after: int | None) -> None:
        """Keep the server's retry hint."""
        super().__init__(status, detail)
        self.retry_after = retry_after


class SolarEdgeCreditLimitError(SolarEdgeResponseError):
    """429 from the monthly credit limit; resets with the billing cycle."""
