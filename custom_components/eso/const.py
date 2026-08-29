"""Constants for the ESO Energy Consumption integration."""

DOMAIN = "eso"

# Local timezone all provider timestamps are expressed in
TIMEZONE = "Europe/Vilnius"

# Account configuration
CONF_OBJECTS = "objects"
CONF_CONSUMED = "consumed"
CONF_RETURNED = "returned"
CONF_COST = "cost"
CONF_PRICE_ENTITY = "price_entity"
CONF_PRICE_CURRENCY = "price_currency"
CONF_FIXED_PRICE = "fixed_price"
CONF_EXPORT_BALANCE = "export_balance"

# Data provider selection
CONF_PROVIDER = "provider"
PROVIDER_ESO = "eso"
PROVIDER_IGNITIS = "ignitis"
PROVIDERS = [PROVIDER_ESO, PROVIDER_IGNITIS]
DEFAULT_PROVIDER = PROVIDER_ESO

# How the Ignitis client authenticates: "direct" posts to the Energy Smart API,
# "sso" goes through the e.ignitis.lt web login like the mobile app does.
CONF_AUTH_TYPE = "auth_type"
AUTH_TYPE_DIRECT = "direct"
AUTH_TYPE_SSO = "sso"
AUTH_TYPES = [AUTH_TYPE_DIRECT, AUTH_TYPE_SSO]
DEFAULT_AUTH_TYPE = AUTH_TYPE_DIRECT

# IMAP (two-factor) configuration
CONF_IMAP = "imap"
CONF_IMAP_HOST = "host"
CONF_IMAP_PORT = "port"
CONF_IMAP_SENDER = "sender"
CONF_IMAP_FOLDER = "folder"
CONF_USE_IMAP = "use_imap"

# Defaults
DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_IMAP_SENDER = "savitarna@eso.lt"
DEFAULT_IMAP_FOLDER = "INBOX"
DEFAULT_PRICE_CURRENCY = "EUR"

# Persisted authenticated session (see ESOClient)
SESSION_FILE = "eso_session.json"

# ESO energy series keys
POWER_CONSUMED = "P+"
POWER_RETURNED = "P-"
ENERGY_TYPE_MAP = {
    CONF_CONSUMED: POWER_CONSUMED,
    CONF_RETURNED: POWER_RETURNED,
}
# Dataset key under which a provider stores the scalar export balance (Ignitis).
EXPORT_BALANCE_KEY = CONF_EXPORT_BALANCE

# ESO daily import: random time inside a window (spreads API load and lets
# multiple HA instances using the same account avoid colliding)
DAILY_IMPORT_WINDOW_START_HOUR = 5
DAILY_IMPORT_WINDOW_START_MINUTE = 10
DAILY_IMPORT_WINDOW_SECONDS = 2 * 3600

# 3 hour pause between fetch retries (ESO)
RETRY_DELAY_SECONDS = 3 * 3600

# Ignitis daily import: fixed time of day plus short data-availability retries
# (Ignitis publishes the previous day's data during the morning, so retry a few
# times until a full 24-hour dataset is available).
IGNITIS_IMPORT_HOUR = 10
IGNITIS_IMPORT_MINUTE = 30
IGNITIS_RETRY_DELAY_SECONDS = 10 * 60
IGNITIS_MAX_RETRIES = 10
# Ignitis serves at most 8 days per request and silently truncates a wider
# window to the most recent days, so backfills are split into weekly requests.
IGNITIS_RANGE_CHUNK_DAYS = 7

# Subentry type: one metering point (object) per subentry
SUBENTRY_TYPE_OBJECT = "object"

# Dispatcher signal fired after the stored-bank series is refreshed
def signal_stored_updated(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_stored_updated"


# Service to trigger an on-demand import
SERVICE_IMPORT_NOW = "import_now"
ATTR_CONFIG_ENTRY_ID = "config_entry_id"
# Optional backfill range for the import; when set, the import fetches the whole
# [date_from, date_to] range instead of the usual daily window.
ATTR_DATE_FROM = "date_from"
ATTR_DATE_TO = "date_to"
