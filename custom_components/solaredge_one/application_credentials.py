"""OAuth 2.0 (Site Access) for SolarEdge ONE for Developers."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from aiohttp import ClientResponseError
from homeassistant.components.application_credentials import (
    AuthImplementation,
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    OAuth2TokenRequestReauthError,
    OAuth2TokenRequestTransientError,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from yarl import URL

from .const import OAUTH2_AUTHORIZE, OAUTH2_TOKEN
from .oauth import async_register_callback_view, callback_url


class SolarEdgeOAuth2Implementation(AuthImplementation):
    """SolarEdge Connect's authorize and token endpoints differ from plain OAuth 2.0."""

    async def async_generate_authorize_url(self, flow_id: str) -> str:
        """Connect reads only client_id and external_id; the flow id rides in external_id.

        Scopes, grant duration and the redirect URL come from the application's
        registration in the developer console, so they are not sent.
        """
        async_register_callback_view(self.hass)
        return str(
            URL(self.authorize_url).with_query(
                {"client_id": self.client_id, "external_id": flow_id}
            )
        )

    async def _token_request(self, data: dict[str, Any]) -> dict[str, Any]:
        """POST the token request as JSON; map failures like the core helper does."""
        payload = {**data, "client_id": self.client_id, "client_secret": self.client_secret}
        session = async_get_clientsession(self.hass)
        resp = await session.post(self.token_url, json=payload)
        try:
            resp.raise_for_status()
        except ClientResponseError as err:
            kwargs: dict[str, Any] = {
                "request_info": err.request_info,
                "history": err.history,
                "status": err.status,
                "message": err.message,
                "headers": err.headers,
                "domain": self.domain,
            }
            if err.status == HTTPStatus.TOO_MANY_REQUESTS or err.status >= 500:
                raise OAuth2TokenRequestTransientError(**kwargs) from err
            raise OAuth2TokenRequestReauthError(**kwargs) from err
        return await resp.json(content_type=None)


async def async_get_auth_implementation(
    hass: HomeAssistant, auth_domain: str, credential: ClientCredential
) -> SolarEdgeOAuth2Implementation:
    """Return the implementation for a stored client ID and secret."""
    async_register_callback_view(hass)
    return SolarEdgeOAuth2Implementation(
        hass,
        auth_domain,
        credential,
        AuthorizationServer(authorize_url=OAUTH2_AUTHORIZE, token_url=OAUTH2_TOKEN),
    )


async def async_get_description_placeholders(hass: HomeAssistant) -> dict[str, str]:
    """Links shown in the Add Application Credentials dialog."""
    return {
        "developer_console": "https://developer.solaredge.com",
        "redirect_url": callback_url(hass),
    }
