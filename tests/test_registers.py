"""Register decoding against a known register image."""

from __future__ import annotations

import math

import pytest

from custom_components.solaredge_one.modbus import registers as reg

from .conftest import build_registers


def _block(mem: dict[int, int], addr: int, count: int) -> list[int]:
    return [mem[addr + i] for i in range(count)]


def test_float_and_u32_roundtrip_low_word_first() -> None:
    """SolarEdge proprietary registers put the low word first."""
    regs = reg.encode_f32_le(1234.5)
    assert reg.f32_le(regs, 0) == pytest.approx(1234.5)
    # 1234.5 = 0x449A5000 -> low word 0x5000 first.
    assert regs == [0x5000, 0x449A]
    assert reg.u32_le(reg.encode_u32_le(86400), 0) == 86400
    assert reg.f32_le([0x0000, 0x7FC0], 0) is None  # NaN = not implemented
    assert not math.isnan(reg.f32_le([0, 0], 0) or 0.0)


def test_decode_common_and_inverter() -> None:
    """SunSpec common block and model 103 inverter."""
    mem = build_registers()
    info = reg.decode_common(_block(mem, reg.COMMON_ADDR, reg.COMMON_COUNT))
    assert (info.manufacturer, info.model, info.serial) == ("SolarEdge", "SE10K-RWS", "7E1234B5")

    inv = reg.decode_inverter(_block(mem, reg.INVERTER_ADDR, reg.INVERTER_COUNT))
    assert inv.phases == 3
    assert inv.ac_current == 15.23
    assert inv.ac_voltage == 401.2
    assert inv.ac_power == 5230
    assert inv.ac_frequency == 50.01
    assert inv.ac_energy == 100000
    assert inv.dc_voltage == 750.0
    assert inv.temperature == 42.1
    assert inv.status == "producing"


def test_decode_meter_signed_power() -> None:
    """Negative meter power means importing from the grid."""
    mem = build_registers()
    meter = reg.decode_meter(_block(mem, reg.METER_ADDRS[0], reg.METER_COUNT))
    assert meter.ac_power == -1500
    assert meter.exported_energy == 50000
    assert meter.imported_energy == 30000
    assert meter.ac_voltage == 230.1


def test_decode_battery() -> None:
    """Battery identity and status blocks."""
    mem = build_registers()
    info, rated = reg.decode_battery_info(
        _block(mem, reg.BATTERY_INFO_ADDRS[0], reg.BATTERY_INFO_COUNT)
    )
    assert info.serial == "BAT0001"
    assert rated == pytest.approx(9700.0)
    status = reg.decode_battery_status(
        _block(
            mem, reg.BATTERY_INFO_ADDRS[0] + reg.BATTERY_STATUS_OFFSET, reg.BATTERY_STATUS_COUNT
        ),
        rated,
    )
    assert status.state_of_energy == pytest.approx(65.0)
    assert status.power == pytest.approx(-2000.0)
    assert status.lifetime_import == 1700000
    assert status.status == "discharging"


def test_missing_devices_raise() -> None:
    """Empty blocks are reported as absent, not decoded as zeros."""
    with pytest.raises(reg.RegisterError):
        reg.decode_meter([0] * reg.METER_COUNT)
    with pytest.raises(reg.RegisterError):
        reg.decode_battery_info([0] * reg.BATTERY_INFO_COUNT)
    with pytest.raises(reg.RegisterError):
        reg.decode_inverter([0] * reg.INVERTER_COUNT)


def test_decode_control() -> None:
    """Storage control block."""
    mem = build_registers()
    control = reg.decode_control(_block(mem, reg.CONTROL_ADDR, reg.CONTROL_COUNT))
    assert control.storage_control_mode == 1
    assert control.backup_reserve == pytest.approx(20.0)
    assert control.remote_command_timeout == 3600
    assert control.export_site_limit == pytest.approx(5000.0)
