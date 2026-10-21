"""Sensors from the cloud API and the optional Modbus connection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import CloudData, Series
from .const import DOMAIN
from .coordinator import CloudCoordinator, ModbusCoordinator, SolarEdgeConfigEntry
from .entity import (
    CloudEntity,
    ModbusEntity,
    child_device,
    cloud_device,
    local_device,
    site_device,
)
from .modbus.registers import BATTERY_STATUS, INVERTER_STATUS, BatteryData, InverterData, MeterData

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class SiteSensorDescription(SensorEntityDescription):
    """A site-level cloud sensor."""

    value_fn: Callable[[CloudData], Any]


def _energy(key: str, fn: Callable[[CloudData], Any], **kw: Any) -> SiteSensorDescription:
    return SiteSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=fn,
        **kw,
    )


def _ov(attr: str) -> Callable[[CloudData], Any]:
    return lambda d: getattr(d.overview, attr) if d.overview else None


SITE_SENSORS: tuple[SiteSensorDescription, ...] = (
    SiteSensorDescription(
        key="current_power",
        translation_key="current_power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda d: d.power.latest_watts if d.power else None,
    ),
    _energy("production_today", _ov("production")),
    _energy("consumption_today", _ov("consumption")),
    _energy("self_consumption_today", _ov("production_to_home")),
    _energy("grid_import_today", _ov("consumption_from_grid")),
    _energy("grid_export_today", _ov("production_to_grid")),
    _energy("storage_charge_today", _ov("production_to_storage")),
    _energy("storage_discharge_today", _ov("consumption_from_storage")),
    _energy("lifetime_production", lambda d: d.lifetime_energy),
    SiteSensorDescription(
        key="open_alerts",
        translation_key="open_alerts",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: len(d.alerts),
    ),
    SiteSensorDescription(
        key="last_update",
        translation_key="last_update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.site.last_update_time,
    ),
)


@dataclass(frozen=True, kw_only=True)
class LocalSensorDescription[T](SensorEntityDescription):
    """A Modbus sensor reading one decoded block."""

    value_fn: Callable[[T], Any]


def _power(key: str, fn: Callable[[Any], Any], **kw: Any) -> LocalSensorDescription[Any]:
    return LocalSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=fn,
        **kw,
    )


def _total_energy(key: str, fn: Callable[[Any], Any]) -> LocalSensorDescription[Any]:
    return LocalSensorDescription(
        key=key,
        translation_key=key,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=fn,
    )


def _electric(key: str, fn: Callable[[Any], Any], kind: str) -> LocalSensorDescription[Any]:
    device_class, unit = {
        "V": (SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT),
        "A": (SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE),
        "Hz": (SensorDeviceClass.FREQUENCY, UnitOfFrequency.HERTZ),
        "C": (SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS),
    }[kind]
    return LocalSensorDescription(
        key=key,
        translation_key=key,
        device_class=device_class,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=unit,
        suggested_display_precision=1,
        entity_registry_enabled_default=kind != "Hz",
        value_fn=fn,
    )


INVERTER_SENSORS: tuple[LocalSensorDescription[InverterData], ...] = (
    _power("ac_power", lambda i: i.ac_power),
    _power("dc_power", lambda i: i.dc_power),
    _total_energy("ac_energy", lambda i: i.ac_energy),
    _electric("ac_voltage", lambda i: i.ac_voltage, "V"),
    _electric("ac_current", lambda i: i.ac_current, "A"),
    _electric("ac_frequency", lambda i: i.ac_frequency, "Hz"),
    _electric("dc_voltage", lambda i: i.dc_voltage, "V"),
    _electric("dc_current", lambda i: i.dc_current, "A"),
    _electric("temperature", lambda i: i.temperature, "C"),
    LocalSensorDescription(
        key="status",
        translation_key="inverter_status",
        device_class=SensorDeviceClass.ENUM,
        options=list(INVERTER_STATUS.values()),
        value_fn=lambda i: i.status,
    ),
)

METER_SENSORS: tuple[LocalSensorDescription[MeterData], ...] = (
    _power("grid_power", lambda m: m.ac_power),
    _total_energy("grid_exported_energy", lambda m: m.exported_energy),
    _total_energy("grid_imported_energy", lambda m: m.imported_energy),
    _electric("ac_voltage", lambda m: m.ac_voltage, "V"),
    _electric("ac_current", lambda m: m.ac_current, "A"),
    _electric("ac_frequency", lambda m: m.ac_frequency, "Hz"),
)

BATTERY_SENSORS: tuple[LocalSensorDescription[BatteryData], ...] = (
    LocalSensorDescription(
        key="state_of_energy",
        translation_key="state_of_energy",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
        value_fn=lambda b: b.state_of_energy,
    ),
    LocalSensorDescription(
        key="state_of_health",
        translation_key="state_of_health",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda b: b.state_of_health,
    ),
    _power("battery_power", lambda b: b.power, suggested_display_precision=0),
    LocalSensorDescription(
        key="available_energy",
        translation_key="available_energy",
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
        suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value_fn=lambda b: b.available_energy,
    ),
    _total_energy("battery_charged_energy", lambda b: b.lifetime_import or None),
    _total_energy("battery_discharged_energy", lambda b: b.lifetime_export or None),
    _electric("battery_voltage", lambda b: b.voltage, "V"),
    _electric("battery_current", lambda b: b.current, "A"),
    _electric("temperature", lambda b: b.temperature, "C"),
    LocalSensorDescription(
        key="status",
        translation_key="battery_status",
        device_class=SensorDeviceClass.ENUM,
        options=list(BATTERY_STATUS.values()),
        value_fn=lambda b: b.status,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolarEdgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the sensors."""
    cloud = entry.runtime_data.cloud
    entities: list[SensorEntity] = [SiteSensor(cloud, d) for d in SITE_SENSORS]
    entities += _telemetry_sensors(cloud)

    if (modbus := entry.runtime_data.modbus) is not None and modbus.inverter_info:
        site = cloud.site_id
        inverter = local_device(cloud, modbus.inverter_info, "INVERTER", "Inverter")
        entities += [
            LocalSensor(modbus, d, inverter, f"{site}_modbus_inverter", lambda data: data.inverter)
            for d in INVERTER_SENSORS
        ]
        for index in modbus.client.meters:
            meter = child_device(
                cloud,
                identifiers={(DOMAIN, f"{site}_modbus_meter_{index}")},
                name=f"Meter {index + 1}",
                manufacturer="SolarEdge",
            )
            entities += [
                LocalSensor(
                    modbus,
                    d,
                    meter,
                    f"{site}_modbus_meter{index}",
                    lambda data, i=index: data.meters.get(i),
                )
                for d in METER_SENSORS
            ]
        for index, (info, _) in modbus.client.batteries.items():
            battery = local_device(cloud, info, "BATTERY", f"Battery {index + 1}")
            entities += [
                LocalSensor(
                    modbus,
                    d,
                    battery,
                    f"{site}_modbus_battery{index}",
                    lambda data, i=index: data.batteries.get(i),
                )
                for d in BATTERY_SENSORS
            ]
    async_add_entities(entities)


class SiteSensor(CloudEntity, SensorEntity):
    """A site-level value from the cloud."""

    entity_description: SiteSensorDescription

    def __init__(self, coordinator: CloudCoordinator, description: SiteSensorDescription) -> None:
        """Attach to the site device."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.site_id}_{description.key}"
        self._attr_device_info = site_device(coordinator)

    @property
    def native_value(self) -> Any:
        """Current value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """List open alerts on the alert counter."""
        if self.entity_description.key != "open_alerts":
            return None
        return {
            "alerts": [
                {
                    "type": a.type,
                    "category": a.category,
                    "impact": a.impact,
                    "component": a.component,
                }
                for a in self.coordinator.data.alerts
            ]
        }


# --- cloud device telemetry (generic: one sensor per metric the API returns) ---

_POWER_UNITS = {"W", "KW", "MW"}
_ENERGY_UNITS = {"WH", "KWH", "MWH", "GWH"}
_PLAIN_UNITS = {
    "VOLT": (SensorDeviceClass.VOLTAGE, UnitOfElectricPotential.VOLT),
    "AMPERE": (SensorDeviceClass.CURRENT, UnitOfElectricCurrent.AMPERE),
    "HERTZ": (SensorDeviceClass.FREQUENCY, UnitOfFrequency.HERTZ),
    "PERCENTAGE": (SensorDeviceClass.BATTERY, PERCENTAGE),
    "PERCENTAGE_100": (SensorDeviceClass.BATTERY, PERCENTAGE),
}


def _humanize(metric: str) -> str:
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", metric).lower()
    return words[:1].upper() + words[1:]


def _telemetry_sensors(cloud: CloudCoordinator) -> list[SensorEntity]:
    data = cloud.data
    devices = {d.serial_number: d for d in data.devices}
    entities: list[SensorEntity] = []
    for kind, block in (
        ("inverters", data.inverters),
        ("meters", data.meters),
        ("storage", data.storage),
    ):
        for serial, metrics in block.items():
            device = (
                cloud_device(cloud, devices[serial]) if serial in devices else site_device(cloud)
            )
            for metric, series in metrics.items():
                if (unit := (series.unit or "").upper()) and (
                    unit in _POWER_UNITS or unit in _ENERGY_UNITS or unit in _PLAIN_UNITS
                ):
                    entities.append(TelemetrySensor(cloud, kind, serial, metric, unit, device))
    return entities


class TelemetrySensor(CloudEntity, SensorEntity):
    """One metric of one device's cloud telemetry (today, hourly buckets)."""

    def __init__(
        self,
        coordinator: CloudCoordinator,
        kind: str,
        serial: str,
        metric: str,
        unit: str,
        device: DeviceInfo,
    ) -> None:
        """Pick device class and aggregation from the metric's unit."""
        super().__init__(coordinator)
        self._kind, self._serial, self._metric = kind, serial, metric
        self._attr_unique_id = f"{serial}_cloud_{metric}"
        self._attr_name = f"{_humanize(metric)} (cloud)"
        self._attr_device_info = device
        self._attr_entity_registry_enabled_default = kind != "inverters" or metric in (
            "power",
            "energy",
        )
        if unit in _POWER_UNITS:
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._value: Callable[[Series], Any] = lambda s: s.latest_watts
        elif unit in _ENERGY_UNITS and "remaining" in metric.lower():
            self._attr_device_class = SensorDeviceClass.ENERGY_STORAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR
            self._attr_suggested_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._value = lambda s: s.latest_watt_hours
        elif unit in _ENERGY_UNITS:
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
            self._attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR
            self._attr_suggested_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._value = lambda s: s.total_watt_hours
        else:
            self._attr_device_class, self._attr_native_unit_of_measurement = _PLAIN_UNITS[unit]
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._value = lambda s: s.latest

    @property
    def native_value(self) -> Any:
        """Latest value or today's total."""
        series = getattr(self.coordinator.data, self._kind).get(self._serial, {}).get(self._metric)
        return None if series is None else self._value(series)


class LocalSensor(ModbusEntity, SensorEntity):
    """A real-time value read over Modbus."""

    entity_description: LocalSensorDescription[Any]

    def __init__(
        self,
        coordinator: ModbusCoordinator,
        description: LocalSensorDescription[Any],
        device: DeviceInfo,
        prefix: str,
        block: Callable[[Any], Any],
    ) -> None:
        """`block` picks the decoded block (inverter, meter n, battery n) from a refresh."""
        super().__init__(coordinator)
        self.entity_description = description
        self._block = block
        self._attr_unique_id = f"{prefix}_{description.key}"
        self._attr_device_info = device

    @property
    def available(self) -> bool:
        """Unavailable when the block is missing from the last read."""
        return super().available and self._block(self.coordinator.data) is not None

    @property
    def native_value(self) -> Any:
        """Current value."""
        block = self._block(self.coordinator.data)
        return None if block is None else self.entity_description.value_fn(block)
