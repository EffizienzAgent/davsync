from datetime import timedelta

import frappe
from frappe import _

from davsync.caldav.ignores import series_uid
from davsync.extensions import load

CALDAV_FIELDS = ("caldav_account", "caldav_uid", "caldav_href", "caldav_etag")


class CalendarTarget:
	"""Where calendar events are imported to. Apps subclass it and name the subclass in the
	`davsync_calendar_target` hook. The doctype needs the fields in CALDAV_FIELDS and `status`."""

	doctype = "Event"
	cancelled_status = "Cancelled"

	def existing(self, account: str, href: str) -> dict[str, str]:
		"""Records imported from one calendar object, by occurrence key."""
		rows = frappe.get_all(
			self.doctype,
			filters={"caldav_account": account, "caldav_href": href},
			fields=["name", "caldav_uid"],
		)
		return {row.caldav_uid: row.name for row in rows}

	def find(self, account: str, key: str) -> str | None:
		return frappe.db.get_value(self.doctype, {"caldav_uid": key, "caldav_account": account})

	def is_protected(self, name: str) -> bool:
		"""Records the ignore rules never hide, e.g. ones another integration owns."""
		return False

	def recurring_hrefs(self, account: str) -> list[str]:
		"""Objects of active recurring series, fetched again daily to extend the horizon."""
		return frappe.get_all(
			self.doctype,
			filters={
				"caldav_account": account,
				"caldav_uid": ("like", "%::%"),
				"caldav_href": ("is", "set"),
				"status": ("!=", self.cancelled_status),
			},
			pluck="caldav_href",
			distinct=True,
		)

	def series(self, account: str, uid: str) -> list:
		rows = frappe.get_all(
			self.doctype,
			filters={"caldav_account": account, "caldav_uid": ("like", f"{uid}%")},
			fields=["name", "caldav_uid", "caldav_href"],
		)
		return [row for row in rows if series_uid(row.caldav_uid) == uid]

	def check_series_permission(self, account: str, uid: str) -> None:
		for row in self.series(account, uid):
			frappe.has_permission(self.doctype, "write", row.name, throw=True)

	def set_ignored(self, account: str, uid: str, ignored: bool) -> list:
		"""Called when a series is added to or removed from Ignored Calendar Event."""
		from davsync.caldav.sync import request_refetch

		rows = self.series(account, uid)
		if ignored:
			for row in rows:
				self.cancel(row.name)
		else:
			request_refetch(account, [row.caldav_href for row in rows if row.caldav_href])
		return rows

	def upsert(self, context, href, etag, event, people, name) -> str:
		"""Create or update the record for one event; returns created, linked, updated or skipped."""
		raise NotImplementedError

	def cancel(self, name: str) -> int:
		"""Mark a record whose event is gone as cancelled; returns 1 when it changed."""
		raise NotImplementedError


class EventTarget(CalendarTarget):
	"""Imports into Frappe's Event, private to the account owner, with Contact participants."""

	def upsert(self, context, href, etag, event, people, name):
		from davsync.caldav.sync import LOCATION_LENGTH, TITLE_LENGTH, fit

		doc = frappe.get_doc("Event", name) if name else frappe.new_doc("Event")
		before = None if doc.is_new() else self.snapshot(doc)
		doc.update(
			{
				"caldav_account": context.account,
				"caldav_uid": event.key,
				"caldav_href": href,
				"caldav_etag": etag,
				"subject": fit(event.summary, TITLE_LENGTH) or _("Appointment"),
				"starts_on": event.start,
				"ends_on": event.end - timedelta(seconds=1) if event.all_day else event.end,
				"all_day": int(event.all_day),
				"location": fit(event.location, LOCATION_LENGTH) or None,
				"description": event.description or None,
				"status": self.status(doc, event),
			}
		)
		if doc.is_new():
			doc.event_type = "Private"
			doc.event_category = "Meeting"
			doc.send_reminder = 0
		if people is not None:
			self.set_participants(doc, people)
		if doc.is_new():
			doc.insert(ignore_permissions=True)
			if context.owner:
				doc.db_set("owner", context.owner, update_modified=False)
			return "created"
		if self.snapshot(doc) == before:
			return "skipped"
		doc.save(ignore_permissions=True)
		return "updated"

	def set_participants(self, doc, people):
		contacts = {}
		for person in people:
			contact = contact_by_email(person.email)
			if contact:
				contacts.setdefault(contact, person.email)
		doc.event_participants = []
		for contact, email in contacts.items():
			doc.append(
				"event_participants",
				{"reference_doctype": "Contact", "reference_docname": contact, "email": email},
			)

	@staticmethod
	def status(doc, event):
		if event.cancelled:
			return "Cancelled"
		return "Open" if doc.status in (None, "", "Cancelled") else doc.status

	@staticmethod
	def snapshot(doc):
		fields = (*CALDAV_FIELDS, "subject", "starts_on", "ends_on", "all_day", "location", "description")
		values = tuple(str(doc.get(field) or "") for field in (*fields, "status"))
		return values, tuple((r.reference_docname, r.email) for r in doc.event_participants)

	def cancel(self, name):
		doc = frappe.get_doc("Event", name)
		doc.caldav_etag = None
		cancelled = doc.status != "Cancelled"
		doc.status = "Cancelled"
		doc.save(ignore_permissions=True)
		return int(cancelled)


def contact_by_email(email):
	names = frappe.get_all(
		"Contact Email",
		filters={"email_id": email, "parenttype": "Contact"},
		pluck="parent",
		order_by="creation, idx",
	)
	return names[0] if names else None


def get_target() -> CalendarTarget:
	return load("davsync_calendar_target", EventTarget)
