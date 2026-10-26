import time

import frappe
from frappe.utils import cint, cstr

DOCTYPE = "DAV Sync Settings"
FALLBACK_REGION = "DE"
DEFAULT_INTERVAL = 5
TICK_SLACK_SECONDS = 30


def settings():
	return frappe.get_cached_doc(DOCTYPE)


def default_country() -> str | None:
	return settings().default_country or frappe.db.get_default("country")


def phone_region() -> str:
	region = settings().phone_region
	if not region and (country := default_country()):
		region = frappe.db.get_value("Country", country, "code")
	return cstr(region or FALLBACK_REGION).strip().upper()


def self_emails() -> list[str]:
	configured = frappe.conf.get("calendar_self_emails")
	if configured:
		return list(configured)
	return [line.strip() for line in cstr(settings().self_emails).splitlines() if line.strip()]


def sync_interval(kind: str) -> int:
	return cint(settings().get(f"{kind}_interval")) or DEFAULT_INTERVAL


def sync_due(kind: str) -> bool:
	"""Called by the per-minute scheduler; true once the configured interval has passed."""
	key = f"davsync-last-run:{kind}"
	last = frappe.cache.get_value(key)
	now = time.time()
	if last and now - last < sync_interval(kind) * 60 - TICK_SLACK_SECONDS:
		return False
	frappe.cache.set_value(key, now)
	return True
