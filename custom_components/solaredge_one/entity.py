"""Base entities and device registry wiring."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import Device
from .const import DOMAIN, MANUFACTURER
from .coordinator import CloudCoordinator, ModbusCoordinator
from .modbus.registers import DeviceInfo as ModbusDeviceInfo


def register_site_device(hass: HomeAssistant, entry: ConfigEntry, cloud: CloudCoordinator) -> str:
    """Register the monitoring site, parent of every other device; return its device id."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, str(cloud.site_id))},
        manufacturer=MANUFACTURER,
        name=cloud.data.site.name,
        model="Monitoring site",
        entry_type=DeviceEntryType.SERVICE,
    )
    return device.id


def site_device(coordinator: CloudCoordinator) -> DeviceInfo:
    """The monitoring site (already registered at setup)."""
    return DeviceInfo(identifiers={(DOMAIN, str(coordinator.site_id))})


def child_device(coordinator: CloudCoordinator, **info: Any) -> DeviceInfo:
    """A device that hangs off the site."""
    return DeviceInfo(**info, via_device_id=coordinator.site_device_id)


def cloud_device(coordinator: CloudCoordinator, device: Device) -> DeviceInfo:
    """A device from the cloud inventory."""
    return child_device(
        coordinator,
        identifiers={(DOMAIN, device.serial_number)},
        manufacturer=device.manufacturer or MANUFACTURER,
        model=device.model,
        name=device.name or f"{device.type.title()} {device.serial_number}",
        serial_number=device.serial_number,
        sw_version=device.firmware_version,
    )


def _cloud_serial(cloud: CloudCoordinator, local_serial: str, kind: str) -> str | None:
    """Match a Modbus serial to the inventory, which may append a check suffix."""
    if not local_serial or cloud.data is None:
        return None
    for device in cloud.data.devices:
        if device.type == kind and device.serial_number.split("-")[0] == local_serial:
            return device.serial_number
    return None


def local_device(
    cloud: CloudCoordinator, info: ModbusDeviceInfo, kind: str, fallback_name: str
) -> DeviceInfo:
    """A device found over Modbus, merged with its inventory entry when serials match."""
    serial = _cloud_serial(cloud, info.serial, kind) or f"modbus_{kind.lower()}_{info.serial}"
    return child_device(
        cloud,
        identifiers={(DOMAIN, serial)},
        manufacturer=info.manufacturer or MANUFACTURER,
        model=info.model or None,
        name=fallback_name,
        serial_number=info.serial or None,
        sw_version=info.version or None,
    )


class CloudEntity(CoordinatorEntity[CloudCoordinator]):
    """An entity fed by the cloud API."""

    _attr_has_entity_name = True


class ModbusEntity(CoordinatorEntity[ModbusCoordinator]):
    """An entity fed by the local Modbus connection."""

    _attr_has_entity_name = True
