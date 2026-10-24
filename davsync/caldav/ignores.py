import glob
from fnmatch import fnmatchcase

import frappe
from frappe.utils import cstr


def normalize(email):
	return cstr(email).strip().lower()


def exact_pattern(email):
	"""A pattern matching only this address, even when it contains glob characters."""
	return glob.escape(normalize(email))


def series_uid(key):
	return cstr(key).partition("::")[0]


def ignored_uids(account):
	return set(frappe.get_all("Ignored Calendar Event", filters={"account": account}, pluck="uid"))


def attendee_patterns(account):
	return frappe.get_all(
		"Ignored Attendee",
		filters={"parenttype": "CardDAV Account", "parent": account},
		pluck="pattern",
		order_by="idx asc",
	)


def is_ignored_attendee(email, patterns):
	email = normalize(email)
	return any(fnmatchcase(email, normalize(pattern)) for pattern in patterns)
