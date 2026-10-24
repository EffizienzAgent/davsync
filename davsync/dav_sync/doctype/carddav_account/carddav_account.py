import frappe
from frappe import _
from frappe.model.document import Document

from davsync.caldav.ignores import normalize


class CardDAVAccount(Document):
	def validate(self):
		self.validate_ignored_attendees()
		if self.is_new():
			return
		url_changed = self.has_value_changed("calendar_url")
		if url_changed or self.has_value_changed("calendar_since"):
			self.calendar_sync_token = None
			self.calendar_fresh = 1
		if url_changed:
			self.calendar_url_changed = 1

	def validate_ignored_attendees(self):
		seen = set()
		for row in self.ignored_attendees:
			row.pattern = normalize(row.pattern)
			local, at, domain = row.pattern.rpartition("@")
			if not (at and local and domain):
				frappe.throw(
					_("{0} is not an e-mail address or a pattern like *@example.com.").format(row.pattern)
				)
			if row.pattern in seen:
				frappe.throw(_("The pattern {0} is listed more than once.").format(row.pattern))
			seen.add(row.pattern)
