"""Typed views of the Monitoring API V2 responses the integration reads."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

_POWER_TO_W = {"W": 1.0, "KW": 1_000.0, "MW": 1_000_000.0}
_ENERGY_TO_WH = {"WH": 1.0, "KWH": 1_000.0, "MWH": 1_000_000.0, "GWH": 1_000_000_000.0}

DEVICE_TYPES = ("INVERTER", "METER", "BATTERY")


def parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp; None for anything unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def to_watts(value: float | None, unit: str | None) -> float | None:
    """Convert power to W; None when the unit is not a power unit."""
    factor = _POWER_TO_W.get(unit.upper()) if unit else None
    return None if value is None or factor is None else value * factor


def to_watt_hours(value: float | None, unit: str | None) -> float | None:
    """Convert energy to Wh; None when the unit is not an energy unit."""
    factor = _ENERGY_TO_WH.get(unit.upper()) if unit else None
    return None if value is None or factor is None else value * factor


@dataclass(frozen=True, slots=True)
class Series:
    """A measurements envelope: `{unit, values: [{timestamp, value}]}`."""

    unit: str | None
    values: tuple[float | None, ...] = ()

    @classmethod
    def from_api(cls, data: Any) -> Series:
        """Build from a measurements object; tolerate missing parts."""
        data = _dict(data)
        raw = data.get("values")
        values = tuple(
            _float(item.get("value"))
            for item in (raw if isinstance(raw, list) else [])
            if isinstance(item, dict)
        )
        unit = data.get("unit")
        return cls(unit=unit if isinstance(unit, str) else None, values=values)

    @property
    def latest(self) -> float | None:
        """Most recent non-null value."""
        return next((v for v in reversed(self.values) if v is not None), None)

    @property
    def total(self) -> float | None:
        """Sum of non-null values; None when there are none."""
        present = [v for v in self.values if v is not None]
        return sum(present) if present else None

    @property
    def latest_watts(self) -> float | None:
        """Latest value in W."""
        return to_watts(self.latest, self.unit)

    @property
    def latest_watt_hours(self) -> float | None:
        """Latest value in Wh (for levels such as remaining battery energy)."""
        return to_watt_hours(self.latest, self.unit)

    @property
    def total_watt_hours(self) -> float | None:
        """Summed value in Wh."""
        return to_watt_hours(self.total, self.unit)


@dataclass(frozen=True, slots=True)
class SiteMetadata:
    """GET /sites/{id}."""

    site_id: int
    name: str
    peak_power_kw: float | None
    installation_date: datetime | None
    last_update_time: datetime | None
    timezone: str | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> SiteMetadata:
        """Build from the site body."""
        return cls(
            site_id=int(data["siteId"]),
            name=str(data.get("name") or data["siteId"]),
            peak_power_kw=_float(data.get("peakPower")),
            installation_date=parse_datetime(data.get("installationDate")),
            last_update_time=parse_datetime(data.get("lastUpdateTime")),
            timezone=_dict(data.get("location")).get("timezone"),
        )


@dataclass(frozen=True, slots=True)
class SiteOverview:
    """GET /sites/{id}/overview for today (site time), every value in Wh."""

    production: float | None = None
    production_to_home: float | None = None
    production_to_storage: float | None = None
    production_to_grid: float | None = None
    consumption: float | None = None
    consumption_from_pv: float | None = None
    consumption_from_storage: float | None = None
    consumption_from_grid: float | None = None

    @classmethod
    def from_api(cls, data: Any) -> SiteOverview:
        """Build from the overview body; each block carries its own unit."""
        data = _dict(data)
        prod = _dict(data.get("production"))
        cons = _dict(data.get("consumption"))

        def wh(block: dict[str, Any], key: str) -> float | None:
            return to_watt_hours(_float(block.get(key)), block.get("unit"))

        return cls(
            production=wh(prod, "total"),
            production_to_home=wh(prod, "toSelfConsumption"),
            production_to_storage=wh(prod, "toStorage"),
            production_to_grid=wh(prod, "toGrid"),
            consumption=wh(cons, "total"),
            consumption_from_pv=wh(cons, "fromPv"),
            consumption_from_storage=wh(cons, "fromStorage"),
            consumption_from_grid=wh(cons, "fromGrid"),
        )


@dataclass(frozen=True, slots=True)
class Device:
    """One entry of GET /sites/{id}/devices."""

    type: str
    serial_number: str
    name: str | None
    manufacturer: str | None
    model: str | None
    firmware_version: str | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Device:
        """Build from an inventory item."""
        return cls(
            type=str(data.get("type", "")),
            serial_number=str(data["serialNumber"]),
            name=data.get("name"),
            manufacturer=data.get("manufacturer"),
            model=data.get("model"),
            firmware_version=data.get("firmwareVersion"),
        )


@dataclass(frozen=True, slots=True)
class Alert:
    """One entry of GET /sites/{id}/alerts."""

    alert_id: int
    type: str | None
    category: str | None
    impact: int | None
    component: str | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Alert:
        """Build from an alert item."""
        impact = data.get("impact")
        return cls(
            alert_id=int(data["alertId"]),
            type=data.get("type"),
            category=data.get("category"),
            impact=impact if isinstance(impact, int) and not isinstance(impact, bool) else None,
            component=_dict(data.get("component")).get("name"),
        )


# Serial number -> metric name (as the API spells it) -> series.
type Telemetry = dict[str, dict[str, Series]]


def parse_telemetry(data: Any, key: str) -> Telemetry:
    """Parse a telemetry body keyed by `inverters`, `meters` or `storage`."""
    devices = _dict(data).get(key)
    return {
        str(serial): {name: Series.from_api(series) for name, series in metrics.items()}
        for serial, metrics in _dict(devices).items()
        if isinstance(metrics, dict)
    }


@dataclass(slots=True)
class CloudData:
    """Everything one cloud refresh knows about a site."""

    site: SiteMetadata
    devices: list[Device] = field(default_factory=list)
    overview: SiteOverview | None = None
    power: Series | None = None
    lifetime_energy: float | None = None
    alerts: list[Alert] = field(default_factory=list)
    inverters: Telemetry = field(default_factory=dict)
    meters: Telemetry = field(default_factory=dict)
    storage: Telemetry = field(default_factory=dict)
