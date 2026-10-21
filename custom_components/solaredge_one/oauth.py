"""OAuth callback for SolarEdge Connect.

SolarEdge Connect reads only `client_id`, `external_id` and `redirect_label`
from the authorize URL. It drops Home Assistant's `state` and `redirect_uri`
and redirects to the URL registered for the application with `code`,
`site_id` and `external_id`. Home Assistant's own callback requires `state`,
so this view receives the redirect instead, using `external_id` to carry the
flow id.
"""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import web
from homeassistant.components import http
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CALLBACK_PATH = f"/api/{DOMAIN}/oauth/callback"
_DATA_VIEW = f"{DOMAIN}_oauth_view"


@callback
def async_register_callback_view(hass: HomeAssistant) -> None:
    """Register the callback view once."""
    if not hass.data.get(_DATA_VIEW):
        hass.http.register_view(SolarEdgeCallbackView())
        hass.data[_DATA_VIEW] = True


def callback_url(hass: HomeAssistant) -> str:
    """The redirect URL to register for the SolarEdge application."""
    req = http.current_request.get()
    if req is not None and (base := req.headers.get("HA-Frontend-Base")):
        return f"{base}{CALLBACK_PATH}"
    try:
        return f"{get_url(hass, prefer_external=True)}{CALLBACK_PATH}"
    except NoURLAvailableError:
        return f"https://<your-home-assistant>{CALLBACK_PATH}"


class SolarEdgeCallbackView(http.HomeAssistantView):
    """Resume the config flow named by `external_id`."""

    requires_auth = False
    url = CALLBACK_PATH
    name = f"api:{DOMAIN}:oauth:callback"

    async def get(self, request: web.Request) -> web.Response:
        """Receive the authorization code."""
        hass: HomeAssistant = request.app[http.KEY_HASS]
        flow_id = request.query.get("external_id")
        flow = next(
            (
                f
                for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
                if f["flow_id"] == flow_id and f.get("step_id") == "auth"
            ),
            None,
        )
        if flow is None:
            return web.Response(
                text="No SolarEdge ONE sign-in is waiting. Start again from Home Assistant.",
                status=400,
            )

        user_input: dict[str, Any] = {
            "state": {"flow_id": flow_id, "redirect_uri": f"{request.url.origin()}{CALLBACK_PATH}"}
        }
        if "code" in request.query:
            user_input["code"] = request.query["code"]
            if site_id := request.query.get("site_id"):
                user_input["site_id"] = site_id
        elif "error" in request.query:
            user_input["error"] = request.query["error"]
        else:
            return web.Response(text="Missing code or error parameter", status=400)

        await hass.config_entries.flow.async_configure(flow_id=flow_id, user_input=user_input)
        _LOGGER.debug("Resumed SolarEdge ONE OAuth flow")
        return web.Response(
            headers={"content-type": "text/html"}, text="<script>window.close()</script>"
        )
