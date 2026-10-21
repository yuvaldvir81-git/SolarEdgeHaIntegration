"""Battery and export limit numbers, written over Modbus."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ModbusCoordinator, SolarEdgeConfigEntry
from .entity import ModbusEntity, local_device
from .modbus import registers as reg

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class ControlNumberDescription(NumberEntityDescription):
    """A float32 or uint32 control register."""

    register: int
    kind: str
    value_fn: Callable[[reg.ControlData], float | None]
    needs: str  # "battery" or "meter"


def _watts(
    key: str,
    register: int,
    fn: Callable[[reg.ControlData], float | None],
    needs: str,
    maximum: float,
) -> ControlNumberDescription:
    return ControlNumberDescription(
        key=key,
        translation_key=key,
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        native_min_value=0,
        native_max_value=maximum,
        native_step=50,
        mode=NumberMode.BOX,
        register=register,
        kind="f32",
        value_fn=fn,
        needs=needs,
    )


NUMBERS: tuple[ControlNumberDescription, ...] = (
    ControlNumberDescription(
        key="backup_reserve",
        translation_key="backup_reserve",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        mode=NumberMode.SLIDER,
        register=reg.REG_BACKUP_RESERVE,
        kind="f32",
        value_fn=lambda c: c.backup_reserve,
        needs="battery",
    ),
    _watts(
        "remote_charge_limit",
        reg.REG_REMOTE_CHARGE_LIMIT,
        lambda c: c.remote_charge_limit,
        "battery",
        20000,
    ),
    _watts(
        "remote_discharge_limit",
        reg.REG_REMOTE_DISCHARGE_LIMIT,
        lambda c: c.remote_discharge_limit,
        "battery",
        20000,
    ),
    ControlNumberDescription(
        key="remote_command_timeout",
        translation_key="remote_command_timeout",
        device_class=NumberDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        native_min_value=0,
        native_max_value=86400,
        native_step=60,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        register=reg.REG_REMOTE_COMMAND_TIMEOUT,
        kind="u32",
        value_fn=lambda c: c.remote_command_timeout,
        needs="battery",
    ),
    ControlNumberDescription(
        key="ac_charge_limit",
        translation_key="ac_charge_limit",
        native_min_value=0,
        native_max_value=100000,
        native_step=1,
        mode=NumberMode.BOX,
        entity_category=EntityCategory.CONFIG,
        entity_registry_enabled_default=False,
        register=reg.REG_AC_CHARGE_LIMIT,
        kind="f32",
        value_fn=lambda c: c.ac_charge_limit,
        needs="battery",
    ),
    _watts(
        "export_limit", reg.REG_EXPORT_SITE_LIMIT, lambda c: c.export_site_limit, "meter", 100000
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolarEdgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the numbers the installation supports when control is enabled."""
    runtime = entry.runtime_data
    modbus = runtime.modbus
    if (
        modbus is None
        or not runtime.allow_control
        or not modbus.client.has_control
        or modbus.inverter_info is None
    ):
        return
    present = {
        "battery": bool(modbus.client.batteries),
        "meter": bool(modbus.client.meters),
    }
    async_add_entities(ControlNumber(modbus, entry, d) for d in NUMBERS if present[d.needs])


class ControlNumber(ModbusEntity, NumberEntity):
    """Number backed by a control register."""

    entity_description: ControlNumberDescription

    def __init__(
        self,
        coordinator: ModbusCoordinator,
        entry: SolarEdgeConfigEntry,
        description: ControlNumberDescription,
    ) -> None:
        """Attach to the inverter, which owns the control registers."""
        super().__init__(coordinator)
        self.entity_description = description
        cloud = entry.runtime_data.cloud
        self._attr_unique_id = f"{cloud.site_id}_modbus_{description.key}"
        self._attr_device_info = local_device(
            cloud, coordinator.inverter_info, "INVERTER", "Inverter"
        )

    @property
    def available(self) -> bool:
        """Unavailable until the control block has been read."""
        return super().available and self.coordinator.data.control is not None

    @property
    def native_value(self) -> float | None:
        """The value the inverter currently holds."""
        control = self.coordinator.data.control
        return None if control is None else self.entity_description.value_fn(control)

    async def async_set_native_value(self, value: float) -> None:
        """Write the value."""
        d = self.entity_description
        await self.coordinator.async_write(
            d.kind, d.register, int(value) if d.kind == "u32" else value
        )
