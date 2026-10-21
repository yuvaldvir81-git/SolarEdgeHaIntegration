"""Config flow: OAuth sign-in, site, then optional local Modbus."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_TOKEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
)
import probatio

from .api import (
    SiteMetadata,
    SolarEdgeAuthenticationError,
    SolarEdgeClient,
    SolarEdgeConnectionError,
    SolarEdgeCreditLimitError,
    SolarEdgeError,
    SolarEdgeForbiddenError,
)
from .const import (
    CONF_DEVICE_TELEMETRY,
    CONF_MODBUS_CONTROL,
    CONF_MODBUS_DEVICE_ID,
    CONF_MODBUS_ENABLED,
    CONF_MODBUS_HOST,
    CONF_MODBUS_PORT,
    CONF_MODBUS_SCAN_SECONDS,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_SITE_ID,
    DEFAULT_DEVICE_TELEMETRY,
    DEFAULT_MODBUS_DEVICE_ID,
    DEFAULT_MODBUS_PORT,
    DEFAULT_MODBUS_SCAN_SECONDS,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
    MAX_MODBUS_SCAN_SECONDS,
    MAX_SCAN_INTERVAL_MINUTES,
    MIN_MODBUS_SCAN_SECONDS,
    MIN_SCAN_INTERVAL_MINUTES,
)

_LOGGER = logging.getLogger(__name__)

SITE_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_SITE_ID): NumberSelector(
            NumberSelectorConfig(min=1, step=1, mode=NumberSelectorMode.BOX)
        )
    }
)


def _box(minimum: int, maximum: int, unit: str | None = None) -> NumberSelector:
    config = NumberSelectorConfig(min=minimum, max=maximum, step=1, mode=NumberSelectorMode.BOX)
    if unit:
        config["unit_of_measurement"] = unit
    return NumberSelector(config)


def _modbus_fields(values: Mapping[str, Any]) -> dict[Any, Any]:
    return {
        probatio.Required(
            CONF_MODBUS_ENABLED, default=values.get(CONF_MODBUS_ENABLED, False)
        ): BooleanSelector(),
        probatio.Optional(
            CONF_MODBUS_HOST, description={"suggested_value": values.get(CONF_MODBUS_HOST)}
        ): TextSelector(),
        probatio.Required(
            CONF_MODBUS_PORT, default=values.get(CONF_MODBUS_PORT, DEFAULT_MODBUS_PORT)
        ): _box(1, 65535),
        probatio.Required(
            CONF_MODBUS_DEVICE_ID,
            default=values.get(CONF_MODBUS_DEVICE_ID, DEFAULT_MODBUS_DEVICE_ID),
        ): _box(1, 247),
        probatio.Required(
            CONF_MODBUS_CONTROL, default=values.get(CONF_MODBUS_CONTROL, False)
        ): BooleanSelector(),
    }


def _normalize_modbus(user_input: Mapping[str, Any]) -> dict[str, Any]:
    return {
        CONF_MODBUS_ENABLED: bool(user_input[CONF_MODBUS_ENABLED]),
        CONF_MODBUS_HOST: (user_input.get(CONF_MODBUS_HOST) or "").strip(),
        CONF_MODBUS_PORT: int(user_input[CONF_MODBUS_PORT]),
        CONF_MODBUS_DEVICE_ID: int(user_input[CONF_MODBUS_DEVICE_ID]),
        CONF_MODBUS_CONTROL: bool(user_input[CONF_MODBUS_CONTROL]),
    }


async def _validate_modbus(hass: HomeAssistant, options: Mapping[str, Any]) -> str | None:
    """Return an error key, or None when the inverter answers (or Modbus is off)."""
    if not options[CONF_MODBUS_ENABLED]:
        return None
    if not options[CONF_MODBUS_HOST]:
        return "modbus_host_required"
    # Imported here so the cloud-only path never needs pymodbus.
    from .modbus.client import ModbusError, SolarEdgeModbus  # noqa: PLC0415
    from .modbus.registers import RegisterError  # noqa: PLC0415

    client = SolarEdgeModbus(
        options[CONF_MODBUS_HOST], options[CONF_MODBUS_PORT], options[CONF_MODBUS_DEVICE_ID]
    )
    try:
        await client.async_discover()
    except (ModbusError, RegisterError) as err:
        _LOGGER.debug("Modbus validation failed: %s", err)
        return "modbus_cannot_connect"
    finally:
        await client.async_close()
    return None


class SolarEdgeOneFlowHandler(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Site Access sign-in for a homeowner's SolarEdge site."""

    DOMAIN = DOMAIN
    VERSION = 1

    def __init__(self) -> None:
        """Initialize flow state."""
        super().__init__()
        self._oauth_data: dict[str, Any] = {}
        self._site: SiteMetadata | None = None
        self._callback_site_id: int | None = None

    @property
    def logger(self) -> logging.Logger:
        """Flow logger."""
        return _LOGGER

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> SolarEdgeOneOptionsFlow:
        """Options flow."""
        return SolarEdgeOneOptionsFlow()

    async def async_step_auth(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Remember the site SolarEdge approved; our callback view passes it along."""
        if user_input is not None and (site_id := user_input.pop(CONF_SITE_ID, None)):
            try:
                self._callback_site_id = int(site_id)
            except ValueError:
                self._callback_site_id = None
        return await super().async_step_auth(user_input)

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Keep the token, then use the approved site (or re-check it on reauth)."""
        self._oauth_data = data
        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            site_id = int(entry.data[CONF_SITE_ID])
            if self._callback_site_id not in (None, site_id):
                return self.async_abort(reason="wrong_site")
            error, _ = await self._async_validate_site(site_id)
            if error:
                return self.async_abort(reason=error)
            return self.async_update_reload_and_abort(entry, data_updates=data)
        if self._callback_site_id is not None:
            return await self.async_step_site({CONF_SITE_ID: self._callback_site_id})
        return await self.async_step_site()

    async def async_step_site(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Validate the site; asked for only when SolarEdge did not send it back."""
        errors: dict[str, str] = {}
        if user_input is not None:
            site_id = int(user_input[CONF_SITE_ID])
            await self.async_set_unique_id(str(site_id))
            self._abort_if_unique_id_configured()
            error, site = await self._async_validate_site(site_id)
            if site is not None:
                self._site = site
                return await self.async_step_modbus()
            errors["base"] = error or "unknown"
        return self.async_show_form(
            step_id="site",
            data_schema=self.add_suggested_values_to_schema(SITE_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_modbus(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Optionally connect to the inverter over the LAN for real-time data and control."""
        assert self._site is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            options = _normalize_modbus(user_input)
            if (error := await _validate_modbus(self.hass, options)) is None:
                return self.async_create_entry(
                    title=self._site.name,
                    data={**self._oauth_data, CONF_SITE_ID: self._site.site_id},
                    options=options,
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="modbus",
            data_schema=probatio.Schema(_modbus_fields(user_input or {})),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The grant expired or was revoked."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm, then sign in again."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_pick_implementation()

    async def _async_validate_site(self, site_id: int) -> tuple[str | None, SiteMetadata | None]:
        token = str(self._oauth_data[CONF_TOKEN]["access_token"])

        async def provider() -> str:
            return token

        client = SolarEdgeClient(async_get_clientsession(self.hass), provider)
        try:
            return None, await client.async_get_site(site_id)
        except SolarEdgeAuthenticationError:
            return "invalid_auth", None
        except SolarEdgeForbiddenError:
            return "site_forbidden", None
        except SolarEdgeCreditLimitError:
            return "credit_limit", None
        except SolarEdgeConnectionError:
            return "cannot_connect", None
        except SolarEdgeError as err:
            _LOGGER.error("Unexpected SolarEdge API response: %s", err)
            return "unknown", None


class SolarEdgeOneOptionsFlow(OptionsFlowWithReload):
    """Cloud polling budget and the local Modbus connection."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show and validate the options."""
        errors: dict[str, str] = {}
        values: Mapping[str, Any] = self.config_entry.options
        if user_input is not None:
            options = {
                CONF_SCAN_INTERVAL_MINUTES: int(user_input[CONF_SCAN_INTERVAL_MINUTES]),
                CONF_DEVICE_TELEMETRY: bool(user_input[CONF_DEVICE_TELEMETRY]),
                CONF_MODBUS_SCAN_SECONDS: int(user_input[CONF_MODBUS_SCAN_SECONDS]),
                **_normalize_modbus(user_input),
            }
            if (error := await _validate_modbus(self.hass, options)) is None:
                return self.async_create_entry(data=options)
            errors["base"] = error
            values = user_input
        schema = probatio.Schema(
            {
                probatio.Required(
                    CONF_SCAN_INTERVAL_MINUTES,
                    default=values.get(CONF_SCAN_INTERVAL_MINUTES, DEFAULT_SCAN_INTERVAL_MINUTES),
                ): _box(MIN_SCAN_INTERVAL_MINUTES, MAX_SCAN_INTERVAL_MINUTES, "min"),
                probatio.Required(
                    CONF_DEVICE_TELEMETRY,
                    default=values.get(CONF_DEVICE_TELEMETRY, DEFAULT_DEVICE_TELEMETRY),
                ): BooleanSelector(),
                **_modbus_fields(values),
                probatio.Required(
                    CONF_MODBUS_SCAN_SECONDS,
                    default=values.get(CONF_MODBUS_SCAN_SECONDS, DEFAULT_MODBUS_SCAN_SECONDS),
                ): _box(MIN_MODBUS_SCAN_SECONDS, MAX_MODBUS_SCAN_SECONDS, "s"),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
