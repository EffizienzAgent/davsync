import frappe
from frappe.utils import cstr

DOCTYPE = "DAV Sync Settings"
FALLBACK_REGION = "DE"


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
