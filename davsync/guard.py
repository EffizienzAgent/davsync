from contextlib import contextmanager

import frappe

CONTACT_FIELDS = (
	"carddav_account",
	"carddav_uid",
	"carddav_href",
	"carddav_etag",
	"carddav_raw",
	"carddav_synced_at",
)
CALDAV_FIELDS = ("caldav_account", "caldav_uid", "caldav_href", "caldav_etag")


@contextmanager
def trusted():
	"""Sync code may write the sync bookkeeping fields; requests may not."""
	previous = frappe.flags.in_davsync
	frappe.flags.in_davsync = True
	try:
		yield
	finally:
		frappe.flags.in_davsync = previous


def is_trusted():
	return bool(frappe.flags.in_davsync or frappe.flags.in_carddav_pull)


def protect(doc, fields):
	"""Keep the stored bookkeeping values; Frappe does not enforce read_only on the server."""
	if is_trusted():
		return
	stored = None if doc.is_new() else frappe.db.get_value(doc.doctype, doc.name, list(fields), as_dict=True)
	for field in fields:
		if doc.meta.has_field(field):
			doc.set(field, stored.get(field) if stored else None)


def protect_contact(doc, method=None):
	protect(doc, CONTACT_FIELDS)


def protect_caldav(doc, method=None):
	protect(doc, CALDAV_FIELDS)
