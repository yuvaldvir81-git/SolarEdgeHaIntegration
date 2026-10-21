"""Battery (StorEdge) mode selects, written over Modbus."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import ModbusCoordinator, SolarEdgeConfigEntry
from .entity import ModbusEntity, local_device
from .modbus import registers as reg

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class ControlSelectDescription(SelectEntityDescription):
    """A uint16 enum register."""

    register: int
    choices: dict[int, str]
    value_fn: Callable[[reg.ControlData], int]


SELECTS: tuple[ControlSelectDescription, ...] = (
    ControlSelectDescription(
        key="storage_control_mode",
        translation_key="storage_control_mode",
        register=reg.REG_STORAGE_CONTROL_MODE,
        choices=reg.STORAGE_CONTROL_MODES,
        value_fn=lambda c: c.storage_control_mode,
    ),
    ControlSelectDescription(
        key="ac_charge_policy",
        translation_key="ac_charge_policy",
        entity_category=EntityCategory.CONFIG,
        register=reg.REG_AC_CHARGE_POLICY,
        choices=reg.AC_CHARGE_POLICIES,
        value_fn=lambda c: c.ac_charge_policy,
    ),
    ControlSelectDescription(
        key="default_command_mode",
        translation_key="default_command_mode",
        entity_category=EntityCategory.CONFIG,
        register=reg.REG_DEFAULT_COMMAND_MODE,
        choices=reg.STORAGE_COMMAND_MODES,
        value_fn=lambda c: c.default_command_mode,
    ),
    ControlSelectDescription(
        key="remote_command_mode",
        translation_key="remote_command_mode",
        register=reg.REG_REMOTE_COMMAND_MODE,
        choices=reg.STORAGE_COMMAND_MODES,
        value_fn=lambda c: c.remote_command_mode,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolarEdgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the selects when control is enabled and a battery answers."""
    runtime = entry.runtime_data
    modbus = runtime.modbus
    if (
        modbus is None
        or not runtime.allow_control
        or not modbus.client.has_control
        or not modbus.client.batteries
        or modbus.inverter_info is None
    ):
        return
    async_add_entities(ControlSelect(modbus, entry, d) for d in SELECTS)


class ControlSelect(ModbusEntity, SelectEntity):
    """Select backed by a storage control register."""

    entity_description: ControlSelectDescription

    def __init__(
        self,
        coordinator: ModbusCoordinator,
        entry: SolarEdgeConfigEntry,
        description: ControlSelectDescription,
    ) -> None:
        """Attach to the inverter, which owns the storage registers."""
        super().__init__(coordinator)
        self.entity_description = description
        cloud = entry.runtime_data.cloud
        self._attr_unique_id = f"{cloud.site_id}_modbus_{description.key}"
        self._attr_device_info = local_device(
            cloud, coordinator.inverter_info, "INVERTER", "Inverter"
        )
        self._attr_options = list(description.choices.values())
        self._codes = {name: code for code, name in description.choices.items()}

    @property
    def available(self) -> bool:
        """Unavailable until the control block has been read."""
        return super().available and self.coordinator.data.control is not None

    @property
    def current_option(self) -> str | None:
        """The mode the inverter currently holds."""
        control = self.coordinator.data.control
        if control is None:
            return None
        return self.entity_description.choices.get(self.entity_description.value_fn(control))

    async def async_select_option(self, option: str) -> None:
        """Write the mode."""
        await self.coordinator.async_write(
            "u16", self.entity_description.register, self._codes[option]
        )
