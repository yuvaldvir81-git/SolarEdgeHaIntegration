"""SolarEdge ONE: Monitoring API V2 over OAuth, plus optional local Modbus control."""

from __future__ import annotations

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_entry_oauth2_flow, config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType
import probatio

from .api import SolarEdgeClient
from .const import (
    CONF_MODBUS_CONTROL,
    CONF_MODBUS_DEVICE_ID,
    CONF_MODBUS_ENABLED,
    CONF_MODBUS_HOST,
    CONF_MODBUS_PORT,
    DEFAULT_MODBUS_DEVICE_ID,
    DEFAULT_MODBUS_PORT,
    DOMAIN,
    SERVICE_SET_STORAGE_COMMAND,
)
from .coordinator import (
    CloudCoordinator,
    ModbusCoordinator,
    SolarEdgeConfigEntry,
    SolarEdgeRuntimeData,
)
from .entity import register_site_device

PLATFORMS = [Platform.NUMBER, Platform.SELECT, Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_MODE = "mode"
ATTR_DURATION = "duration"
ATTR_CHARGE_LIMIT = "charge_limit"
ATTR_DISCHARGE_LIMIT = "discharge_limit"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services."""
    # Imported lazily so cloud-only installs never load pymodbus.
    from .modbus import registers as reg  # noqa: PLC0415

    command_codes = {name: code for code, name in reg.STORAGE_COMMAND_MODES.items()}

    async def set_storage_command(call: ServiceCall) -> None:
        """Run a timed remote charge/discharge command, then fall back to the default mode."""
        entry: SolarEdgeConfigEntry | None = hass.config_entries.async_get_entry(
            call.data[ATTR_CONFIG_ENTRY_ID]
        )
        if entry is None or entry.domain != DOMAIN or not hasattr(entry, "runtime_data"):
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="entry_not_loaded"
            )
        runtime = entry.runtime_data
        modbus = runtime.modbus
        if modbus is None or not runtime.allow_control or not modbus.client.batteries:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="control_unavailable"
            )
        # Order matters: the command and limits apply only in Remote Control mode,
        # and the timeout bounds how long the inverter stays in it.
        await modbus.async_write("u16", reg.REG_STORAGE_CONTROL_MODE, 4, refresh=False)
        await modbus.async_write(
            "u32", reg.REG_REMOTE_COMMAND_TIMEOUT, call.data[ATTR_DURATION], refresh=False
        )
        if ATTR_CHARGE_LIMIT in call.data:
            await modbus.async_write(
                "f32", reg.REG_REMOTE_CHARGE_LIMIT, call.data[ATTR_CHARGE_LIMIT], refresh=False
            )
        if ATTR_DISCHARGE_LIMIT in call.data:
            await modbus.async_write(
                "f32",
                reg.REG_REMOTE_DISCHARGE_LIMIT,
                call.data[ATTR_DISCHARGE_LIMIT],
                refresh=False,
            )
        await modbus.async_write(
            "u16", reg.REG_REMOTE_COMMAND_MODE, command_codes[call.data[ATTR_MODE]]
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_STORAGE_COMMAND,
        set_storage_command,
        schema=probatio.Schema(
            {
                probatio.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
                probatio.Required(ATTR_MODE): probatio.In(list(command_codes)),
                probatio.Required(ATTR_DURATION): probatio.All(
                    probatio.Coerce(int), probatio.Range(min=60, max=86400)
                ),
                probatio.Optional(ATTR_CHARGE_LIMIT): probatio.All(
                    probatio.Coerce(float), probatio.Range(min=0)
                ),
                probatio.Optional(ATTR_DISCHARGE_LIMIT): probatio.All(
                    probatio.Coerce(float), probatio.Range(min=0)
                ),
            }
        ),
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SolarEdgeConfigEntry) -> bool:
    """Set up one site."""
    implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
        hass, entry
    )
    session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)

    async def token() -> str:
        await session.async_ensure_token_valid()
        return str(session.token["access_token"])

    cloud = CloudCoordinator(hass, entry, SolarEdgeClient(async_get_clientsession(hass), token))
    await cloud.async_config_entry_first_refresh()
    cloud.site_device_id = register_site_device(hass, entry, cloud)

    modbus: ModbusCoordinator | None = None
    options = entry.options
    if options.get(CONF_MODBUS_ENABLED) and options.get(CONF_MODBUS_HOST):
        from .modbus.client import SolarEdgeModbus  # noqa: PLC0415

        client = SolarEdgeModbus(
            options[CONF_MODBUS_HOST],
            options.get(CONF_MODBUS_PORT, DEFAULT_MODBUS_PORT),
            options.get(CONF_MODBUS_DEVICE_ID, DEFAULT_MODBUS_DEVICE_ID),
        )
        modbus = ModbusCoordinator(hass, entry, client)
        entry.async_on_unload(client.async_close)
        await modbus.async_config_entry_first_refresh()

    entry.runtime_data = SolarEdgeRuntimeData(
        cloud=cloud,
        modbus=modbus,
        allow_control=bool(options.get(CONF_MODBUS_CONTROL)),
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SolarEdgeConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
