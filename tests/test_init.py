"""Entry setup, sensors, controls and the storage command service."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.solaredge_one.const import API_BASE_URL, DOMAIN
from custom_components.solaredge_one.modbus import registers as reg

from .conftest import SITE_ID, config_entry, mock_cloud

MODBUS = {
    "modbus_enabled": True,
    "modbus_host": "192.168.1.50",
    "modbus_port": 1502,
    "modbus_device_id": 1,
    "modbus_control": True,
}


def _entity_id(hass: HomeAssistant, domain: str, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id, unique_id
    return entity_id


def _state(hass: HomeAssistant, domain: str, unique_id: str) -> str:
    return hass.states.get(_entity_id(hass, domain, unique_id)).state


@pytest.mark.usefixtures("setup_credentials")
async def test_cloud_only(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Cloud sensors come from the overview, power and energy endpoints."""
    mock_cloud(aioclient_mock)
    entry = config_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED

    assert _state(hass, "sensor", f"{SITE_ID}_current_power") == "5230.5"
    assert float(_state(hass, "sensor", f"{SITE_ID}_production_today")) == 24.5
    assert float(_state(hass, "sensor", f"{SITE_ID}_grid_import_today")) == 1.0
    assert float(_state(hass, "sensor", f"{SITE_ID}_lifetime_production")) == 50000.0
    assert _state(hass, "sensor", f"{SITE_ID}_open_alerts") == "1"
    # No Modbus: no controls.
    assert not hass.states.async_entity_ids("select")
    assert not hass.states.async_entity_ids("number")

    # Window bounds are Instants (UTC, Z suffix); the API rejects offset-free times.
    energy_url = next(c[1] for c in aioclient_mock.mock_calls if c[1].path.endswith("/energy"))
    assert energy_url.query["from"] == "2022-02-28T22:00:00Z"
    assert energy_url.query["to"].endswith("Z")

    # Requests carry the bearer token, never a query-string key.
    for _, url, _, headers in aioclient_mock.mock_calls:
        assert "api_key" not in str(url)
        assert headers["Authorization"] == "Bearer access"


@pytest.mark.usefixtures("setup_credentials")
async def test_refresh_stays_in_credit_budget(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """After the first refresh, a poll costs 2 calls; the slow group waits 12 hours."""
    mock_cloud(aioclient_mock)
    entry = config_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert aioclient_mock.call_count == 6

    await entry.runtime_data.cloud.async_refresh()
    assert aioclient_mock.call_count == 8
    paths = [str(c[1]).split("?")[0].rsplit("/", 1)[-1] for c in aioclient_mock.mock_calls[-2:]]
    assert paths == ["overview", "power"]


@pytest.mark.usefixtures("setup_credentials")
async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 401 puts the entry into reauth."""
    aioclient_mock.get(f"{API_BASE_URL}/sites/{SITE_ID}", status=401, json={"detail": "expired"})
    entry = config_entry()
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert any(
        f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress()
    )


@pytest.mark.usefixtures("setup_credentials")
async def test_modbus_sensors_and_controls(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fake_inverter
) -> None:
    """Local sensors, the merged inverter device, selects and numbers."""
    mock_cloud(aioclient_mock)
    entry = config_entry(MODBUS)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    prefix = f"{SITE_ID}_modbus"
    assert _state(hass, "sensor", f"{prefix}_inverter_ac_power") == "5230"
    assert _state(hass, "sensor", f"{prefix}_inverter_status") == "producing"
    assert _state(hass, "sensor", f"{prefix}_meter0_grid_power") == "-1500"
    assert _state(hass, "sensor", f"{prefix}_battery0_state_of_energy") == "65.0"
    assert _state(hass, "sensor", f"{prefix}_battery0_status") == "discharging"

    # The Modbus inverter serial (7E1234B5) merges with the cloud's 7E1234B5-67.
    entity = er.async_get(hass).async_get(_entity_id(hass, "sensor", f"{prefix}_inverter_ac_power"))
    device = dr.async_get(hass).async_get(entity.device_id)
    assert (DOMAIN, "7E1234B5-67") in device.identifiers

    assert _state(hass, "select", f"{prefix}_storage_control_mode") == "maximize_self_consumption"
    assert float(_state(hass, "number", f"{prefix}_backup_reserve")) == 20.0
    assert float(_state(hass, "number", f"{prefix}_export_limit")) == 5000.0

    await hass.services.async_call(
        "select",
        "select_option",
        {
            "entity_id": _entity_id(hass, "select", f"{prefix}_storage_control_mode"),
            "option": "backup_only",
        },
        blocking=True,
    )
    assert (reg.REG_STORAGE_CONTROL_MODE, [3]) in fake_inverter.writes

    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": _entity_id(hass, "number", f"{prefix}_backup_reserve"), "value": 35},
        blocking=True,
    )
    assert (reg.REG_BACKUP_RESERVE, reg.encode_f32_le(35.0)) in fake_inverter.writes
    await hass.async_block_till_done()
    assert float(_state(hass, "number", f"{prefix}_backup_reserve")) == 35.0
    assert _state(hass, "select", f"{prefix}_storage_control_mode") == "backup_only"


@pytest.mark.usefixtures("setup_credentials")
async def test_storage_command_service(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fake_inverter
) -> None:
    """The service enters remote control, sets the timeout and limits, then the command."""
    mock_cloud(aioclient_mock)
    entry = config_entry(MODBUS)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    await hass.services.async_call(
        DOMAIN,
        "set_storage_command",
        {
            "config_entry_id": entry.entry_id,
            "mode": "charge_from_pv_and_ac",
            "duration": 1800,
            "charge_limit": 3000,
        },
        blocking=True,
    )
    assert fake_inverter.writes == [
        (reg.REG_STORAGE_CONTROL_MODE, [4]),
        (reg.REG_REMOTE_COMMAND_TIMEOUT, reg.encode_u32_le(1800)),
        (reg.REG_REMOTE_CHARGE_LIMIT, reg.encode_f32_le(3000.0)),
        (reg.REG_REMOTE_COMMAND_MODE, [3]),
    ]


@pytest.mark.usefixtures("setup_credentials")
async def test_control_off_is_read_only(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fake_inverter
) -> None:
    """Without the control option there are no writable entities and the service refuses."""
    mock_cloud(aioclient_mock)
    entry = config_entry({**MODBUS, "modbus_control": False})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert not hass.states.async_entity_ids("select")
    assert not hass.states.async_entity_ids("number")
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "set_storage_command",
            {"config_entry_id": entry.entry_id, "mode": "off", "duration": 600},
            blocking=True,
        )
    assert fake_inverter.writes == []


@pytest.mark.usefixtures("setup_credentials")
async def test_unload(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, fake_inverter
) -> None:
    """Unloading releases the entry."""
    mock_cloud(aioclient_mock)
    entry = config_entry(MODBUS)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
