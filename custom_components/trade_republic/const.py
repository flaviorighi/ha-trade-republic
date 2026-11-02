"""Constants for the Trade Republic integration."""

from datetime import timedelta

DOMAIN = "trade_republic"

CONF_PHONE = "phone"
CONF_HISTORY_IMPORTED = "history_imported"

DEFAULT_SCAN_INTERVAL = timedelta(minutes=30)
MIN_SCAN_MINUTES = 15
MAX_SCAN_MINUTES = 24 * 60
