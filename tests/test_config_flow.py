"""OAuth config flow, site step and optional Modbus step."""

from __future__ import annotations

from http import HTTPStatus
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.solaredge_one.const import (
    API_BASE_URL,
    DOMAIN,
    OAUTH2_AUTHORIZE,
    OAUTH2_TOKEN,
)
from custom_components.solaredge_one.oauth import CALLBACK_PATH

from .conftest import CLIENT_ID, SITE, SITE_ID, build_registers

NO_MODBUS = {
    "modbus_enabled": False,
    "modbus_port": 1502,
    "modbus_device_id": 1,
    "modbus_control": False,
}


async def _start_and_authorize(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    callback_query: str = f"code=abcd&site_id={SITE_ID}",
) -> str:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    flow_id = result["flow_id"]
    # Connect reads only client_id and external_id from the authorize URL.
    assert result["url"] == f"{OAUTH2_AUTHORIZE}?client_id={CLIENT_ID}&external_id={flow_id}"

    # Connect redirects with code, site_id and external_id, never state.
    client = await hass_client_no_auth()
    resp = await client.get(f"{CALLBACK_PATH}?{callback_query}&external_id={flow_id}")
    assert resp.status == HTTPStatus.OK

    aioclient_mock.post(
        OAUTH2_TOKEN,
        json={
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": 7200,
            "token_type": "Bearer",
        },
    )
    return flow_id


@pytest.mark.usefixtures("current_request_with_host", "setup_credentials")
async def test_full_flow_cloud_only(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Sign in; the site comes from the callback; skip Modbus."""
    flow_id = await _start_and_authorize(hass, hass_client_no_auth, aioclient_mock)
    aioclient_mock.get(f"{API_BASE_URL}/sites/{SITE_ID}", json=SITE)

    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["step_id"] == "modbus"

    with patch_setup():
        result = await hass.config_entries.flow.async_configure(flow_id, NO_MODBUS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Home"
    assert result["data"]["site_id"] == SITE_ID
    assert result["data"]["token"]["access_token"] == "access"
    assert result["options"]["modbus_enabled"] is False

    # The token request used a JSON body with the client credentials.
    token_call = next(c for c in aioclient_mock.mock_calls if str(c[1]) == OAUTH2_TOKEN)
    assert token_call[2]["client_id"] == CLIENT_ID
    assert token_call[2]["grant_type"] == "authorization_code"


@pytest.mark.usefixtures("current_request_with_host", "setup_credentials")
async def test_site_forbidden(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """Without site_id on the callback, the site is asked for and validated."""
    flow_id = await _start_and_authorize(
        hass, hass_client_no_auth, aioclient_mock, callback_query="code=abcd"
    )
    aioclient_mock.get(f"{API_BASE_URL}/sites/{SITE_ID}", status=403, json={"detail": "No access"})
    result = await hass.config_entries.flow.async_configure(flow_id)
    assert result["step_id"] == "site"
    result = await hass.config_entries.flow.async_configure(flow_id, {"site_id": SITE_ID})
    assert result["errors"] == {"base": "site_forbidden"}


@pytest.mark.usefixtures("current_request_with_host", "setup_credentials")
async def test_modbus_step_validates(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    aioclient_mock: AiohttpClientMocker,
    fake_inverter,
) -> None:
    """Modbus needs a host, and the inverter must answer."""
    flow_id = await _start_and_authorize(hass, hass_client_no_auth, aioclient_mock)
    aioclient_mock.get(f"{API_BASE_URL}/sites/{SITE_ID}", json=SITE)
    await hass.config_entries.flow.async_configure(flow_id)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {**NO_MODBUS, "modbus_enabled": True}
    )
    assert result["errors"] == {"base": "modbus_host_required"}

    fake_inverter.memory = {}
    result = await hass.config_entries.flow.async_configure(
        flow_id, {**NO_MODBUS, "modbus_enabled": True, "modbus_host": "192.168.1.50"}
    )
    assert result["errors"] == {"base": "modbus_cannot_connect"}

    fake_inverter.memory = build_registers()
    with patch_setup():
        result = await hass.config_entries.flow.async_configure(
            flow_id,
            {
                **NO_MODBUS,
                "modbus_enabled": True,
                "modbus_host": "192.168.1.50",
                "modbus_control": True,
            },
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"]["modbus_host"] == "192.168.1.50"
    assert result["options"]["modbus_control"] is True


@pytest.mark.usefixtures("current_request_with_host", "setup_credentials")
async def test_callback_rejects_unknown_flow(
    hass: HomeAssistant, hass_client_no_auth: ClientSessionGenerator
) -> None:
    """A redirect that names no waiting flow is refused."""
    await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    client = await hass_client_no_auth()
    resp = await client.get(f"{CALLBACK_PATH}?code=abcd&external_id=nope")
    assert resp.status == HTTPStatus.BAD_REQUEST


def patch_setup():
    """Skip entry setup after the flow creates it."""
    return patch("custom_components.solaredge_one.async_setup_entry", return_value=True)
