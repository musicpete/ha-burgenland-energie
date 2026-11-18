"""Constants for the Burgenland Energie integration."""

DOMAIN = "burgenland_energie"
MANUFACTURER = "Burgenland Energie"

CONF_REFRESH_TOKEN = "refresh_token"
CONF_CUSTOMER_NR = "customer_nr"
CONF_SAVE_INVOICES = "save_invoices"
CONF_METER_DAYS = "meter_days"

DEFAULT_METER_DAYS = 45
POLL_INTERVAL_H = 6
METER_IMPORT_TIME = (8, 43)  # local time, once a day
STARTUP_DELAY_S = 90
MAX_INVOICE_DOWNLOADS_PER_RUN = 12
INVOICE_DIR = "burgenland_energie"

SERVICE_DOWNLOAD_INVOICE = "download_invoice"
SERVICE_IMPORT_METER_DATA = "import_meter_data"
ATTR_BILL_NR = "bill_nr"
ATTR_ALL_MISSING = "all_missing"
METER_DATA_STALE_DAYS = 75
ATTR_DAYS = "days"

SIGNAL_METER_UPDATED = f"{DOMAIN}_meter_updated"
