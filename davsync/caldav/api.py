import frappe
from frappe import _

from davsync.caldav.client import CalDAVClient
from davsync.carddav.api import start
from davsync.carddav.client import CardDAVError


@frappe.whitelist(methods=["POST"])
def discover_calendar(account: str) -> str:
	frappe.only_for("System Manager")
	doc = frappe.get_doc("CardDAV Account", account)
	try:
		url = CalDAVClient(account).discover_calendar(doc.calendar_name)
	except CardDAVError as exc:
		frappe.throw(_("Calendar could not be found: {0}").format(exc))
	doc.calendar_url = url
	doc.save()
	return _("Calendar found: {0}").format(url)


@frappe.whitelist(methods=["POST"])
def start_calendar_sync(account: str) -> None:
	start(account, "calendar-sync", "davsync.caldav.sync.run_pull")


@frappe.whitelist(methods=["POST"])
def start_calendar_import(account: str) -> None:
	start(account, "calendar-import", "davsync.caldav.sync.run_pull", initial=True)
