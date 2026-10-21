"""SolarEdge Modbus TCP register map and decoding (no pymodbus imports).

Addresses are 0-based (the SolarEdge documentation's 1-based number minus 1).
SunSpec registers (40000+) are big-endian. SolarEdge's proprietary registers
(0xE000+) hold float32 and uint32/uint64 with the low word first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import struct

# --- SunSpec common, inverter and meter blocks ------------------------------
COMMON_ADDR = 40000
COMMON_COUNT = 69
INVERTER_ADDR = 40069
INVERTER_COUNT = 40
METER_ADDRS = (40188, 40362, 40536)
METER_COUNT = 55

# --- SolarEdge proprietary --------------------------------------------------
CONTROL_ADDR = 0xE000
CONTROL_COUNT = 18
BATTERY_INFO_ADDRS = (0xE100, 0xE200)
BATTERY_INFO_COUNT = 68
BATTERY_STATUS_OFFSET = 0x6C
BATTERY_STATUS_COUNT = 28

# Writable control registers.
REG_EXPORT_SITE_LIMIT = 0xE002  # float32 W
REG_STORAGE_CONTROL_MODE = 0xE004  # uint16
REG_AC_CHARGE_POLICY = 0xE005  # uint16
REG_AC_CHARGE_LIMIT = 0xE006  # float32, kWh or % depending on the policy
REG_BACKUP_RESERVE = 0xE008  # float32 %
REG_DEFAULT_COMMAND_MODE = 0xE00A  # uint16
REG_REMOTE_COMMAND_TIMEOUT = 0xE00B  # uint32 s
REG_REMOTE_COMMAND_MODE = 0xE00D  # uint16
REG_REMOTE_CHARGE_LIMIT = 0xE00E  # float32 W
REG_REMOTE_DISCHARGE_LIMIT = 0xE010  # float32 W

STORAGE_CONTROL_MODES: dict[int, str] = {
    0: "disabled",
    1: "maximize_self_consumption",
    2: "time_of_use",
    3: "backup_only",
    4: "remote_control",
}
AC_CHARGE_POLICIES: dict[int, str] = {
    0: "disabled",
    1: "always_allowed",
    2: "fixed_energy_limit",
    3: "percent_of_production",
}
STORAGE_COMMAND_MODES: dict[int, str] = {
    0: "off",
    1: "charge_excess_pv",
    2: "charge_from_pv_first",
    3: "charge_from_pv_and_ac",
    4: "maximize_export",
    5: "discharge_to_match_load",
    7: "maximize_self_consumption",
}
INVERTER_STATUS: dict[int, str] = {
    1: "off",
    2: "sleeping",
    3: "starting",
    4: "producing",
    5: "throttled",
    6: "shutting_down",
    7: "fault",
    8: "standby",
}
BATTERY_STATUS: dict[int, str] = {
    0: "off",
    1: "standby",
    2: "init",
    3: "charging",
    4: "discharging",
    5: "fault",
    7: "idle",
    10: "power_saving",
}

_U16_NA = 0xFFFF
_S16_NA = -0x8000
_ACC32_NA = 0


class RegisterError(ValueError):
    """A block did not decode as expected (wrong model, missing device)."""


# --- primitive decoders ------------------------------------------------------


def u16(regs: list[int], i: int) -> int:
    """Unsigned 16-bit."""
    return regs[i] & 0xFFFF


def s16(regs: list[int], i: int) -> int:
    """Signed 16-bit."""
    v = regs[i] & 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def u32_be(regs: list[int], i: int) -> int:
    """Unsigned 32-bit, high word first (SunSpec acc32)."""
    return (u16(regs, i) << 16) | u16(regs, i + 1)


def u32_le(regs: list[int], i: int) -> int:
    """Unsigned 32-bit, low word first (SolarEdge proprietary)."""
    return (u16(regs, i + 1) << 16) | u16(regs, i)


def u64_le(regs: list[int], i: int) -> int:
    """Unsigned 64-bit, low word first."""
    return sum(u16(regs, i + k) << (16 * k) for k in range(4))


def f32_le(regs: list[int], i: int) -> float | None:
    """IEEE float32, low word first; None for NaN/inf (not implemented)."""
    raw = struct.pack(">HH", u16(regs, i + 1), u16(regs, i))
    value = struct.unpack(">f", raw)[0]
    return None if math.isnan(value) or math.isinf(value) else value


def string(regs: list[int], i: int, count: int) -> str:
    """ASCII packed two characters per register."""
    raw = b"".join(struct.pack(">H", u16(regs, i + k)) for k in range(count))
    return raw.replace(b"\xff", b"").split(b"\x00", 1)[0].decode("ascii", "ignore").strip()


def encode_f32_le(value: float) -> list[int]:
    """Two registers for a float32, low word first."""
    hi, lo = struct.unpack(">HH", struct.pack(">f", float(value)))
    return [lo, hi]


def encode_u32_le(value: int) -> list[int]:
    """Two registers for a uint32, low word first."""
    value = int(value) & 0xFFFFFFFF
    return [value & 0xFFFF, value >> 16]


def _scaled(value: int, sf: int, not_implemented: int) -> float | None:
    if value == not_implemented or sf == _S16_NA:
        return None
    if sf >= 0:
        return value * 10**sf
    return round(value * 10.0**sf, -sf)


def _scaled_u16(regs: list[int], i: int, sf: int) -> float | None:
    return _scaled(u16(regs, i), sf, _U16_NA)


def _scaled_s16(regs: list[int], i: int, sf: int) -> float | None:
    return _scaled(s16(regs, i), sf, _S16_NA)


def _scaled_acc32(regs: list[int], i: int, sf: int) -> float | None:
    return _scaled(u32_be(regs, i), sf, _ACC32_NA)


# --- decoded blocks ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """Identity of the inverter, a meter or a battery."""

    manufacturer: str
    model: str
    version: str
    serial: str


@dataclass(frozen=True, slots=True)
class InverterData:
    """SunSpec inverter model 101/102/103."""

    phases: int
    ac_current: float | None
    ac_voltage: float | None
    ac_power: float | None
    ac_frequency: float | None
    ac_energy: float | None
    dc_current: float | None
    dc_voltage: float | None
    dc_power: float | None
    temperature: float | None
    status: str | None


@dataclass(frozen=True, slots=True)
class MeterData:
    """SunSpec meter model 201-204. Power is positive when exporting to the grid."""

    ac_current: float | None
    ac_voltage: float | None
    ac_frequency: float | None
    ac_power: float | None
    exported_energy: float | None
    imported_energy: float | None


@dataclass(frozen=True, slots=True)
class BatteryData:
    """SolarEdge battery status block. Power is positive when charging."""

    rated_energy: float | None
    temperature: float | None
    voltage: float | None
    current: float | None
    power: float | None
    lifetime_export: int | None
    lifetime_import: int | None
    max_energy: float | None
    available_energy: float | None
    state_of_health: float | None
    state_of_energy: float | None
    status: str | None


@dataclass(frozen=True, slots=True)
class ControlData:
    """Export limit and StorEdge control registers (0xE000-0xE011)."""

    export_control_mode: int
    export_site_limit: float | None
    storage_control_mode: int
    ac_charge_policy: int
    ac_charge_limit: float | None
    backup_reserve: float | None
    default_command_mode: int
    remote_command_timeout: int
    remote_command_mode: int
    remote_charge_limit: float | None
    remote_discharge_limit: float | None


@dataclass(slots=True)
class ModbusData:
    """One Modbus refresh."""

    inverter: InverterData
    meters: dict[int, MeterData] = field(default_factory=dict)
    batteries: dict[int, BatteryData] = field(default_factory=dict)
    control: ControlData | None = None


def decode_common(regs: list[int]) -> DeviceInfo:
    """SunSpec common block at 40000 (inverter identity)."""
    if string(regs, 0, 2) != "SunS":
        raise RegisterError("No SunSpec marker at 40000")
    return DeviceInfo(
        manufacturer=string(regs, 4, 16),
        model=string(regs, 20, 16),
        version=string(regs, 44, 8),
        serial=string(regs, 52, 16),
    )


def decode_inverter(regs: list[int]) -> InverterData:
    """SunSpec inverter block starting at the model ID register (40069)."""
    model = u16(regs, 0)
    if model not in (101, 102, 103):
        raise RegisterError(f"Unexpected inverter model {model}")
    a_sf, v_sf, w_sf = s16(regs, 6), s16(regs, 13), s16(regs, 15)
    status = u16(regs, 38)
    return InverterData(
        phases=model - 100,
        ac_current=_scaled_u16(regs, 2, a_sf),
        # Model 101 reports the phase voltage in AN (index 10); 3-phase uses AB (7).
        ac_voltage=_scaled_u16(regs, 10 if model == 101 else 7, v_sf),
        ac_power=_scaled_s16(regs, 14, w_sf),
        ac_frequency=_scaled_u16(regs, 16, s16(regs, 17)),
        ac_energy=_scaled_acc32(regs, 24, s16(regs, 26)),
        dc_current=_scaled_u16(regs, 27, s16(regs, 28)),
        dc_voltage=_scaled_u16(regs, 29, s16(regs, 30)),
        dc_power=_scaled_s16(regs, 31, s16(regs, 32)),
        temperature=_scaled_s16(regs, 34, s16(regs, 37)),
        status=INVERTER_STATUS.get(status),
    )


def decode_meter(regs: list[int]) -> MeterData:
    """SunSpec meter block starting at its model ID register."""
    model = u16(regs, 0)
    if model not in (201, 202, 203, 204):
        raise RegisterError(f"No meter (model {model})")
    e_sf = s16(regs, 54)
    return MeterData(
        ac_current=_scaled_s16(regs, 2, s16(regs, 6)),
        ac_voltage=_scaled_s16(regs, 7, s16(regs, 15)),
        ac_frequency=_scaled_s16(regs, 16, s16(regs, 17)),
        ac_power=_scaled_s16(regs, 18, s16(regs, 22)),
        exported_energy=_scaled_acc32(regs, 38, e_sf),
        imported_energy=_scaled_acc32(regs, 46, e_sf),
    )


def decode_battery_info(regs: list[int]) -> tuple[DeviceInfo, float | None]:
    """Battery identity block (0xE100 / 0xE200) and its rated energy in Wh."""
    info = DeviceInfo(
        manufacturer=string(regs, 0, 16),
        model=string(regs, 16, 16),
        version=string(regs, 32, 16),
        serial=string(regs, 48, 16),
    )
    rated = f32_le(regs, 66)
    if not info.serial and not info.model:
        raise RegisterError("No battery")
    return info, rated


def decode_battery_status(regs: list[int], rated_energy: float | None) -> BatteryData:
    """Battery status block (0xE16C / 0xE26C)."""
    soe = f32_le(regs, 24)
    if soe is not None and not 0 <= soe <= 100:
        raise RegisterError(f"Battery state of energy out of range: {soe}")
    status = u32_le(regs, 26)
    return BatteryData(
        rated_energy=rated_energy,
        temperature=f32_le(regs, 0),
        voltage=f32_le(regs, 4),
        current=f32_le(regs, 6),
        power=f32_le(regs, 8),
        lifetime_export=u64_le(regs, 10),
        lifetime_import=u64_le(regs, 14),
        max_energy=f32_le(regs, 18),
        available_energy=f32_le(regs, 20),
        state_of_health=f32_le(regs, 22),
        state_of_energy=soe,
        status=BATTERY_STATUS.get(status),
    )


def decode_control(regs: list[int]) -> ControlData:
    """Export limit and storage control block at 0xE000."""
    return ControlData(
        export_control_mode=u16(regs, 0),
        export_site_limit=f32_le(regs, 2),
        storage_control_mode=u16(regs, 4),
        ac_charge_policy=u16(regs, 5),
        ac_charge_limit=f32_le(regs, 6),
        backup_reserve=f32_le(regs, 8),
        default_command_mode=u16(regs, 10),
        remote_command_timeout=u32_le(regs, 11),
        remote_command_mode=u16(regs, 13),
        remote_charge_limit=f32_le(regs, 14),
        remote_discharge_limit=f32_le(regs, 16),
    )
