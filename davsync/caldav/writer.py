from urllib.parse import quote, urlparse

import frappe
from frappe import _

from davsync.caldav.client import CalDAVClient
from davsync.caldav.compose import OutgoingEvent, compose
from davsync.caldav.sync import Context, cancel_href, forget, pull_one, refetch, remember
from davsync.carddav.client import PreconditionFailed

SCHEDULING_TTL = 3600


def require_write(account):
	values = frappe.db.get_value(
		"CardDAV Account", account, ["calendar_write_enabled", "calendar_url"], as_dict=True
	)
	if not values or not values.calendar_write_enabled:
		frappe.throw(_("Writing to the calendar is not enabled for this account."))
	if not values.calendar_url:
		frappe.throw(_("No calendar is set for this account."))
	return values.calendar_url


def object_path(href):
	return urlparse(href).path


def write_event(
	account: str, event: OutgoingEvent, href: str | None = None, etag: str | None = None
) -> tuple[str, str]:
	calendar_url = require_write(account)
	href = object_path(href or f"{calendar_url}{quote(event.uid, safe='')}.ics")
	text = compose(event)
	try:
		new_etag = CalDAVClient(account).put_event(href, text, etag)
	except PreconditionFailed:
		refetch(account, [href])
		raise
	pull_one(Context(account), href, new_etag, text)
	remember(account, href, new_etag)
	return href, new_etag


def remove_event(account: str, href: str, etag: str) -> None:
	require_write(account)
	href = object_path(href)
	CalDAVClient(account).delete_event(href, etag)
	cancel_href(account, href)
	forget(account, href)


def scheduling(account: str) -> bool:
	key = f"davsync:scheduling:{account}"
	cached = frappe.cache.get_value(key)
	if cached is None:
		cached = int(CalDAVClient(account).supports_scheduling())
		frappe.cache.set_value(key, cached, expires_in_sec=SCHEDULING_TTL)
	return bool(cached)
