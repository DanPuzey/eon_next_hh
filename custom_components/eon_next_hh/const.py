"""Constants for the E.ON Next half-hourly integration."""

from datetime import timedelta

DOMAIN = "eon_next_hh"

CONF_REFRESH_TOKEN = "refresh_token"
CONF_ACCOUNT_NUMBER = "account_number"
CONF_PROPERTY_ID = "property_id"
CONF_MPAN = "mpan"
CONF_SERIAL = "serial"
CONF_BACKFILL_DAYS = "backfill_days"
CONF_OFFPEAK_WINDOWS = "offpeak_windows"

DEFAULT_BACKFILL_DAYS = 180

UPDATE_INTERVAL = timedelta(hours=4)
REFETCH_WINDOW = timedelta(days=3)
DIAGNOSTIC_WINDOW = timedelta(days=7)
FETCH_CHUNK = timedelta(days=7)
BACKFILL_REQUEST_DELAY = 2.0

SERVICE_BACKFILL = "backfill"
ATTR_START_DATE = "start_date"
ATTR_END_DATE = "end_date"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
