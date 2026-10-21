"""Constants for the SolarEdge ONE integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "solaredge_one"
MANUFACTURER: Final = "SolarEdge"

# SolarEdge ONE for Developers, Monitoring API V2 (Site Access / OAuth 2.0).
API_BASE_URL: Final = "https://monitoringapi.solaredge.com/v2"
OAUTH2_AUTHORIZE: Final = "https://connect.solaredge.com/authorize"
OAUTH2_TOKEN: Final = f"{API_BASE_URL}/oauth2/token"

CONF_SITE_ID: Final = "site_id"

# Cloud polling. The free tier allows 2,000 calls a month. Each refresh costs
# 2 calls (overview, power), plus 4 every 12 hours (site, lifetime energy,
# inventory, alerts): ~1,680 a month at 60 minutes. Device telemetry adds 3
# calls per refresh, which needs a paid tier or a longer interval.
CONF_SCAN_INTERVAL_MINUTES: Final = "scan_interval_minutes"
CONF_DEVICE_TELEMETRY: Final = "device_telemetry"
DEFAULT_SCAN_INTERVAL_MINUTES: Final = 60
MIN_SCAN_INTERVAL_MINUTES: Final = 15
MAX_SCAN_INTERVAL_MINUTES: Final = 1440
DEFAULT_DEVICE_TELEMETRY: Final = False

# Optional local Modbus TCP connection (real-time data and control).
CONF_MODBUS_ENABLED: Final = "modbus_enabled"
CONF_MODBUS_HOST: Final = "modbus_host"
CONF_MODBUS_PORT: Final = "modbus_port"
CONF_MODBUS_DEVICE_ID: Final = "modbus_device_id"
CONF_MODBUS_SCAN_SECONDS: Final = "modbus_scan_seconds"
CONF_MODBUS_CONTROL: Final = "modbus_control"
DEFAULT_MODBUS_PORT: Final = 1502
DEFAULT_MODBUS_DEVICE_ID: Final = 1
DEFAULT_MODBUS_SCAN_SECONDS: Final = 10
MIN_MODBUS_SCAN_SECONDS: Final = 5
MAX_MODBUS_SCAN_SECONDS: Final = 300

SERVICE_SET_STORAGE_COMMAND: Final = "set_storage_command"
