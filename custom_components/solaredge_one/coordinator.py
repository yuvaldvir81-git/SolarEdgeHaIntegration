"""Data update coordinators for the cloud API and the local Modbus connection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    CloudData,
    SolarEdgeAuthenticationError,
    SolarEdgeClient,
    SolarEdgeCreditLimitError,
    SolarEdgeError,
    SolarEdgeRateLimitError,
)
from .const import (
    CONF_DEVICE_TELEMETRY,
    CONF_MODBUS_SCAN_SECONDS,
    CONF_SCAN_INTERVAL_MINUTES,
    CONF_SITE_ID,
    DEFAULT_DEVICE_TELEMETRY,
    DEFAULT_MODBUS_SCAN_SECONDS,
    DEFAULT_SCAN_INTERVAL_MINUTES,
    DOMAIN,
)
from .modbus.client import ModbusError, SolarEdgeModbus
from .modbus.registers import ModbusData, RegisterError

_LOGGER = logging.getLogger(__name__)

# Site metadata, lifetime energy, inventory and alerts change slowly; refreshing
# them twice a day instead of every poll keeps a site inside the free tier.
SLOW_REFRESH = timedelta(hours=12)


@dataclass(slots=True)
class SolarEdgeRuntimeData:
    """What a loaded config entry holds."""

    cloud: CloudCoordinator
    modbus: ModbusCoordinator | None
    allow_control: bool


type SolarEdgeConfigEntry = ConfigEntry[SolarEdgeRuntimeData]


class CloudCoordinator(DataUpdateCoordinator[CloudData]):
    """Poll the Monitoring API V2 within the monthly credit budget."""

    config_entry: SolarEdgeConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: SolarEdgeConfigEntry, client: SolarEdgeClient
    ) -> None:
        """Interval and telemetry come from the entry options."""
        minutes = entry.options.get(CONF_SCAN_INTERVAL_MINUTES, DEFAULT_SCAN_INTERVAL_MINUTES)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} cloud {entry.data[CONF_SITE_ID]}",
            update_interval=timedelta(minutes=minutes),
        )
        self.client = client
        self.site_id = int(entry.data[CONF_SITE_ID])
        self.telemetry = entry.options.get(CONF_DEVICE_TELEMETRY, DEFAULT_DEVICE_TELEMETRY)
        self.site_device_id: str | None = None  # set once the site device is registered
        self._slow_at: datetime | None = None

    def _site_tz(self, data: CloudData) -> tzinfo:
        """The site's time zone; "today" (midnight) is measured in it."""
        if data.site.timezone:
            try:
                return ZoneInfo(data.site.timezone)
            except ZoneInfoNotFoundError:
                pass
        return dt_util.get_default_time_zone()

    async def _async_update_data(self) -> CloudData:
        try:
            return await self._fetch()
        except SolarEdgeAuthenticationError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SolarEdgeRateLimitError as err:
            raise UpdateFailed(str(err), retry_after=err.retry_after) from err
        except SolarEdgeCreditLimitError as err:
            raise UpdateFailed(f"Monthly API credit limit reached: {err}") from err
        except SolarEdgeError as err:
            raise UpdateFailed(str(err)) from err

    async def _fetch(self) -> CloudData:
        previous = self.data
        now_utc = dt_util.utcnow()
        slow_due = previous is None or self._slow_at is None or now_utc >= self._slow_at
        sid = self.site_id

        if slow_due:
            data = CloudData(site=await self.client.async_get_site(sid))
            now = dt_util.now(self._site_tz(data))
            if (start := data.site.installation_date) is not None:
                if start.tzinfo is None:
                    start = start.replace(tzinfo=now.tzinfo)
                data.lifetime_energy = await self.client.async_get_lifetime_energy(sid, start, now)
            data.devices = await self.client.async_get_devices(sid)
            data.alerts = await self.client.async_get_open_alerts(sid)
            self._slow_at = now_utc + SLOW_REFRESH
        else:
            data = CloudData(
                site=previous.site,
                devices=previous.devices,
                lifetime_energy=previous.lifetime_energy,
                alerts=previous.alerts,
            )
            now = dt_util.now(self._site_tz(data))

        data.overview = await self.client.async_get_overview(sid)
        data.power = await self.client.async_get_power(sid, now - timedelta(hours=1), now)
        if self.telemetry:
            midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
            data.inverters = await self.client.async_get_telemetry(sid, "inverters", midnight, now)
            data.meters = await self.client.async_get_telemetry(sid, "meters", midnight, now)
            data.storage = await self.client.async_get_telemetry(sid, "storage", midnight, now)
        return data


class ModbusCoordinator(DataUpdateCoordinator[ModbusData]):
    """Poll the inverter locally and apply control writes."""

    config_entry: SolarEdgeConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: SolarEdgeConfigEntry, client: SolarEdgeModbus
    ) -> None:
        """Interval comes from the entry options."""
        seconds = entry.options.get(CONF_MODBUS_SCAN_SECONDS, DEFAULT_MODBUS_SCAN_SECONDS)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} modbus {client.host}",
            update_interval=timedelta(seconds=seconds),
        )
        self.client = client
        self.inverter_info = None

    async def _async_setup(self) -> None:
        try:
            self.inverter_info = await self.client.async_discover()
        except (ModbusError, RegisterError) as err:
            raise UpdateFailed(f"Modbus discovery failed: {err}") from err

    async def _async_update_data(self) -> ModbusData:
        try:
            return await self.client.async_read()
        except (ModbusError, RegisterError) as err:
            raise UpdateFailed(f"Modbus read failed: {err}") from err

    async def async_write(
        self, kind: str, address: int, value: float, *, refresh: bool = True
    ) -> None:
        """Write one control value, then read back so entities show what the inverter holds."""
        writer = {
            "u16": self.client.async_write_u16,
            "u32": self.client.async_write_u32,
            "f32": self.client.async_write_f32,
        }[kind]
        try:
            await writer(address, value)
        except ModbusError as err:
            raise HomeAssistantError(f"SolarEdge rejected the write: {err}") from err
        if refresh:
            # Not the debounced request: a second write within the cooldown would
            # otherwise show the old value until the next poll.
            await self.async_refresh()
