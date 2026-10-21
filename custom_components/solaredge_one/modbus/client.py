"""Async Modbus TCP access to a SolarEdge inverter.

The inverter accepts one Modbus TCP client at a time, so every read and write
goes through one lock and one connection.
"""

from __future__ import annotations

import asyncio
import inspect
import logging

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.exceptions import ModbusException

from . import registers as reg

_LOGGER = logging.getLogger(__name__)
TIMEOUT = 5.0


class ModbusError(Exception):
    """Connection failure or a Modbus exception response."""


def _device_kwarg() -> str:
    """pymodbus renamed `slave` to `device_id` in 3.10."""
    params = inspect.signature(AsyncModbusTcpClient.read_holding_registers).parameters
    return "device_id" if "device_id" in params else "slave"


class SolarEdgeModbus:
    """Read and write one inverter (and the meters/batteries behind it)."""

    def __init__(self, host: str, port: int, device_id: int) -> None:
        """Create a client; the connection opens lazily."""
        self.host = host
        self.port = port
        self._device = {_device_kwarg(): device_id}
        self._client = AsyncModbusTcpClient(host, port=port, timeout=TIMEOUT)
        self._lock = asyncio.Lock()
        self.meters: tuple[int, ...] = ()
        self.batteries: dict[int, tuple[reg.DeviceInfo, float | None]] = {}
        self.has_control = False

    async def _ensure_connected(self) -> None:
        if not self._client.connected and not await self._client.connect():
            raise ModbusError(f"Cannot connect to {self.host}:{self.port}")

    async def _read(self, address: int, count: int) -> list[int]:
        await self._ensure_connected()
        try:
            result = await self._client.read_holding_registers(address, count=count, **self._device)
        except (ModbusException, OSError) as err:
            self._client.close()
            raise ModbusError(f"Read {address:#x} failed: {err}") from err
        if result.isError():
            raise ModbusError(f"Read {address:#x} returned {result}")
        return list(result.registers)

    async def _try_read(self, address: int, count: int) -> list[int] | None:
        """Read a block that may not exist on this installation."""
        try:
            return await self._read(address, count)
        except ModbusError as err:
            _LOGGER.debug("Optional block %#x unavailable: %s", address, err)
            return None

    async def async_close(self) -> None:
        """Drop the connection so other clients can use the inverter."""
        async with self._lock:
            self._client.close()

    async def async_discover(self) -> reg.DeviceInfo:
        """Identify the inverter and find which meters and batteries answer."""
        async with self._lock:
            info = reg.decode_common(await self._read(reg.COMMON_ADDR, reg.COMMON_COUNT))
            reg.decode_inverter(await self._read(reg.INVERTER_ADDR, reg.INVERTER_COUNT))
            meters = []
            for index, addr in enumerate(reg.METER_ADDRS):
                regs = await self._try_read(addr, reg.METER_COUNT)
                try:
                    if regs is not None:
                        reg.decode_meter(regs)
                        meters.append(index)
                except reg.RegisterError:
                    pass
            self.meters = tuple(meters)
            self.batteries = {}
            for index, addr in enumerate(reg.BATTERY_INFO_ADDRS):
                regs = await self._try_read(addr, reg.BATTERY_INFO_COUNT)
                try:
                    if regs is not None:
                        self.batteries[index] = reg.decode_battery_info(regs)
                except reg.RegisterError:
                    pass
            self.has_control = await self._try_read(reg.CONTROL_ADDR, reg.CONTROL_COUNT) is not None
            return info

    async def async_read(self) -> reg.ModbusData:
        """Read everything discovery found."""
        async with self._lock:
            data = reg.ModbusData(
                inverter=reg.decode_inverter(
                    await self._read(reg.INVERTER_ADDR, reg.INVERTER_COUNT)
                )
            )
            for index in self.meters:
                data.meters[index] = reg.decode_meter(
                    await self._read(reg.METER_ADDRS[index], reg.METER_COUNT)
                )
            for index, (_, rated) in self.batteries.items():
                addr = reg.BATTERY_INFO_ADDRS[index] + reg.BATTERY_STATUS_OFFSET
                data.batteries[index] = reg.decode_battery_status(
                    await self._read(addr, reg.BATTERY_STATUS_COUNT), rated
                )
            if self.has_control:
                data.control = reg.decode_control(
                    await self._read(reg.CONTROL_ADDR, reg.CONTROL_COUNT)
                )
            return data

    async def async_write(self, address: int, values: list[int]) -> None:
        """Write consecutive holding registers."""
        async with self._lock:
            await self._ensure_connected()
            try:
                result = await self._client.write_registers(address, values, **self._device)
            except (ModbusException, OSError) as err:
                self._client.close()
                raise ModbusError(f"Write {address:#x} failed: {err}") from err
            if result.isError():
                raise ModbusError(f"Write {address:#x} rejected: {result}")

    async def async_write_u16(self, address: int, value: int) -> None:
        """Write one uint16 register."""
        await self.async_write(address, [int(value) & 0xFFFF])

    async def async_write_f32(self, address: int, value: float) -> None:
        """Write a float32 (low word first)."""
        await self.async_write(address, reg.encode_f32_le(value))

    async def async_write_u32(self, address: int, value: int) -> None:
        """Write a uint32 (low word first)."""
        await self.async_write(address, reg.encode_u32_le(value))
