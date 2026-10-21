"""Shared fixtures: cloud API mocks and a fake SolarEdge inverter on Modbus."""

from __future__ import annotations

from collections.abc import Generator
import time
from typing import Any
from unittest.mock import patch

from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.solaredge_one.const import API_BASE_URL, DOMAIN
from custom_components.solaredge_one.modbus import registers as reg

SITE_ID = 1234567
CLIENT_ID = "client-id"
CLIENT_SECRET = "client-secret"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components in every test."""


@pytest.fixture
async def setup_credentials(hass: HomeAssistant) -> None:
    """Store application credentials."""
    assert await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(
        hass, DOMAIN, ClientCredential(CLIENT_ID, CLIENT_SECRET), DOMAIN
    )


# --- cloud ------------------------------------------------------------------

SITE = {
    "siteId": SITE_ID,
    "name": "Home",
    "peakPower": 10.0,
    "installationDate": "2022-03-01T00:00:00+02:00",
    "lastUpdateTime": "2026-10-21T12:45:00+03:00",
    "location": {"timezone": "Asia/Jerusalem"},
}
OVERVIEW = {
    "siteId": SITE_ID,
    "production": {
        "total": 24.5,
        "unit": "KWH",
        "toSelfConsumption": 12.0,
        "toStorage": 8.0,
        "toGrid": 4.5,
    },
    "consumption": {
        "total": 15000,
        "unit": "WH",
        "fromPv": 12000,
        "fromStorage": 2000,
        "fromGrid": 1000,
    },
}
POWER = {
    "unit": "W",
    "resolution": "QUARTER_HOUR",
    "values": [
        {"timestamp": "2026-10-21T12:15:00+03:00", "value": 5100.0},
        {"timestamp": "2026-10-21T12:30:00+03:00", "value": 5230.5},
        {"timestamp": "2026-10-21T12:45:00+03:00", "value": None},
    ],
}
ENERGY = {
    "unit": "MWH",
    "resolution": "YEAR",
    "values": [{"value": 9.5}, {"value": 15.0}, {"value": 14.0}, {"value": 11.5}, {"value": None}],
}
DEVICES = [
    {
        "type": "INVERTER",
        "serialNumber": "7E1234B5-67",
        "manufacturer": "SolarEdge",
        "model": "SE10K",
        "name": "Inverter 1",
    },
    {
        "type": "BATTERY",
        "serialNumber": "BAT0001-AA",
        "manufacturer": "SolarEdge",
        "model": "Home Battery",
        "name": "Battery 1",
    },
]
ALERTS = [
    {
        "alertId": 1,
        "type": "Low SoE",
        "category": "STORAGE",
        "impact": 3,
        "component": {"name": "Battery 1"},
    },
]


def mock_cloud(aioclient_mock: AiohttpClientMocker) -> None:
    """Register every cloud endpoint the coordinator calls."""
    base = f"{API_BASE_URL}/sites/{SITE_ID}"
    aioclient_mock.get(base, json=SITE)
    aioclient_mock.get(f"{base}/overview", json=OVERVIEW)
    aioclient_mock.get(f"{base}/power", json=POWER)
    aioclient_mock.get(f"{base}/energy", json=ENERGY)
    aioclient_mock.get(f"{base}/devices", json=DEVICES)
    aioclient_mock.get(f"{base}/alerts", json=ALERTS)


def token(expires_in: int = 7200) -> dict[str, Any]:
    """An OAuth token as Home Assistant stores it."""
    return {
        "access_token": "access",
        "refresh_token": "refresh",
        "token_type": "Bearer",
        "expires_in": expires_in,
        "expires_at": time.time() + expires_in,
    }


def config_entry(options: dict[str, Any] | None = None) -> MockConfigEntry:
    """A configured site."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Home",
        unique_id=str(SITE_ID),
        data={"auth_implementation": DOMAIN, "token": token(), "site_id": SITE_ID},
        options=options or {},
    )


# --- fake inverter ---------------------------------------------------------------


def _string(text: str, count: int) -> list[int]:
    raw = text.encode().ljust(count * 2, b"\x00")
    return [int.from_bytes(raw[i : i + 2], "big") for i in range(0, count * 2, 2)]


def _s16(value: int) -> int:
    return value & 0xFFFF


def build_registers(*, battery: bool = True, meter: bool = True) -> dict[int, int]:
    """Register image of a three-phase inverter with a grid meter and a battery."""
    mem: dict[int, int] = {}

    def put(addr: int, values: list[int]) -> None:
        for i, v in enumerate(values):
            mem[addr + i] = v & 0xFFFF

    common = [0] * reg.COMMON_COUNT
    common[0:2] = _string("SunS", 2)
    common[2], common[3] = 1, 65
    common[4:20] = _string("SolarEdge", 16)
    common[20:36] = _string("SE10K-RWS", 16)
    common[44:52] = _string("0004.0020.0036", 8)
    common[52:68] = _string("7E1234B5", 16)
    put(reg.COMMON_ADDR, common)

    inv = [0] * reg.INVERTER_COUNT
    inv[0], inv[1] = 103, 50
    inv[2], inv[6] = 1523, _s16(-2)  # 15.23 A
    inv[7], inv[13] = 4012, _s16(-1)  # 401.2 V
    inv[14], inv[15] = 5230, 0  # 5230 W
    inv[16], inv[17] = 5001, _s16(-2)  # 50.01 Hz
    inv[24], inv[25], inv[26] = 0x0001, 0x86A0, 0  # 100000 Wh
    inv[27], inv[28] = 1400, _s16(-2)
    inv[29], inv[30] = 7500, _s16(-1)
    inv[31], inv[32] = 5300, 0
    inv[34], inv[37] = 4210, _s16(-2)  # 42.10 C
    inv[38] = 4
    put(reg.INVERTER_ADDR, inv)

    if meter:
        m = [0] * reg.METER_COUNT
        m[0], m[1] = 203, 105
        m[2], m[6] = 52, _s16(-1)
        m[7], m[15] = 2301, _s16(-1)
        m[16], m[17] = 5000, _s16(-2)
        m[18], m[22] = _s16(-1500), 0  # importing 1500 W
        m[38], m[39] = 0, 50000
        m[46], m[47] = 0, 30000
        m[54] = 0
        put(reg.METER_ADDRS[0], m)

    control = [0] * reg.CONTROL_COUNT
    control[0] = 1
    control[2:4] = reg.encode_f32_le(5000.0)
    control[4] = 1
    control[5] = 1
    control[6:8] = reg.encode_f32_le(0.0)
    control[8:10] = reg.encode_f32_le(20.0)
    control[10] = 7
    control[11:13] = reg.encode_u32_le(3600)
    control[13] = 7
    control[14:16] = reg.encode_f32_le(5000.0)
    control[16:18] = reg.encode_f32_le(5000.0)
    put(reg.CONTROL_ADDR, control)

    if battery:
        info = [0] * reg.BATTERY_INFO_COUNT
        info[0:16] = _string("SolarEdge", 16)
        info[16:32] = _string("BAT-10K1P", 16)
        info[32:48] = _string("DSP 1.0", 16)
        info[48:64] = _string("BAT0001", 16)
        info[66:68] = reg.encode_f32_le(9700.0)
        put(reg.BATTERY_INFO_ADDRS[0], info)
        status = [0] * reg.BATTERY_STATUS_COUNT
        status[0:2] = reg.encode_f32_le(28.5)
        status[4:6] = reg.encode_f32_le(52.1)
        status[6:8] = reg.encode_f32_le(-38.4)
        status[8:10] = reg.encode_f32_le(-2000.0)
        status[10:14] = [1500000 & 0xFFFF, 1500000 >> 16, 0, 0]
        status[14:18] = [1700000 & 0xFFFF, 1700000 >> 16, 0, 0]
        status[18:20] = reg.encode_f32_le(9600.0)
        status[20:22] = reg.encode_f32_le(6240.0)
        status[22:24] = reg.encode_f32_le(99.0)
        status[24:26] = reg.encode_f32_le(65.0)
        status[26:28] = reg.encode_u32_le(4)
        put(reg.BATTERY_INFO_ADDRS[0] + reg.BATTERY_STATUS_OFFSET, status)
    return mem


class _Result:
    def __init__(self, registers: list[int] | None) -> None:
        self.registers = registers or []
        self._error = registers is None

    def isError(self) -> bool:  # noqa: N802 - pymodbus API
        return self._error


class FakeModbusClient:
    """Stands in for pymodbus.AsyncModbusTcpClient over a register dict."""

    memory: dict[int, int] = {}
    writes: list[tuple[int, list[int]]] = []

    def __init__(self, host: str, port: int = 502, timeout: float = 3) -> None:
        self.connected = False

    async def connect(self) -> bool:
        self.connected = True
        return True

    def close(self) -> None:
        self.connected = False

    async def read_holding_registers(
        self, address: int, *, count: int = 1, device_id: int = 1
    ) -> _Result:
        addrs = range(address, address + count)
        if any(a not in self.memory for a in addrs):
            return _Result(None)
        return _Result([self.memory[a] for a in addrs])

    async def write_registers(
        self, address: int, values: list[int], *, device_id: int = 1
    ) -> _Result:
        FakeModbusClient.writes.append((address, list(values)))
        for i, v in enumerate(values):
            self.memory[address + i] = v
        return _Result(list(values))


@pytest.fixture
def fake_inverter() -> Generator[type[FakeModbusClient]]:
    """Patch pymodbus with the fake inverter."""
    FakeModbusClient.memory = build_registers()
    FakeModbusClient.writes = []
    with patch(
        "custom_components.solaredge_one.modbus.client.AsyncModbusTcpClient", FakeModbusClient
    ):
        yield FakeModbusClient
