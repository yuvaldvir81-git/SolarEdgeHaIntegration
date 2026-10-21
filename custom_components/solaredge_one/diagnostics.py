"""Diagnostics with credentials redacted."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_TOKEN
from homeassistant.core import HomeAssistant

from .const import CONF_MODBUS_HOST
from .coordinator import SolarEdgeConfigEntry

TO_REDACT = {CONF_TOKEN, "access_token", "refresh_token", CONF_MODBUS_HOST}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SolarEdgeConfigEntry
) -> dict[str, Any]:
    """Entry data, options and the last refresh of each coordinator."""
    runtime = entry.runtime_data
    modbus = runtime.modbus
    return {
        "data": async_redact_data(dict(entry.data), TO_REDACT),
        "options": async_redact_data(dict(entry.options), TO_REDACT),
        "cloud": asdict(runtime.cloud.data) if runtime.cloud.data else None,
        "modbus": {
            "meters": list(modbus.client.meters),
            "batteries": sorted(modbus.client.batteries),
            "has_control": modbus.client.has_control,
            "data": asdict(modbus.data) if modbus.data else None,
        }
        if modbus
        else None,
    }
