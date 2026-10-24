import frappe
from frappe import _

from davsync.carddav.client import CardDAVClient, CardDAVError


@frappe.whitelist(methods=["POST"])
def discover(account: str) -> str:
	frappe.only_for("System Manager")
	try:
		url = CardDAVClient(account).discover_address_book()
	except CardDAVError as exc:
		frappe.throw(_("Address book could not be found: {0}").format(exc))
	frappe.db.set_value("CardDAV Account", account, "address_book_url", url)
	return _("Address book found: {0}").format(url)


def start(account, action, method, **kwargs):
	frappe.only_for("System Manager")
	if not frappe.db.exists("CardDAV Account", account):
		frappe.throw(_("CardDAV Account {0} not found").format(account))
	frappe.enqueue(
		method,
		queue="long",
		timeout=3600,
		job_id=f"carddav-{action}-{account}",
		deduplicate=True,
		account=account,
		**kwargs,
	)


@frappe.whitelist(methods=["POST"])
def start_sync(account: str) -> None:
	start(account, "sync", "davsync.carddav.pull.run_pull")


@frappe.whitelist(methods=["POST"])
def start_initial_import(account: str) -> None:
	start(account, "initial-import", "davsync.carddav.pull.run_pull", initial=True)


@frappe.whitelist(methods=["POST"])
def push_all(account: str) -> None:
	start(account, "push-all", "davsync.carddav.push.run_push_all")
