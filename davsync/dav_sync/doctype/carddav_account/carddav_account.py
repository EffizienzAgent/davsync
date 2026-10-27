from urllib.parse import urlparse

import frappe
from frappe import _
from frappe.model.document import Document

from davsync.caldav.ignores import normalize
from davsync.carddav.client import same_origin

LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class CardDAVAccount(Document):
	def validate(self):
		self.validate_urls()
		self.validate_ignored_attendees()
		if self.calendar_write_enabled and not self.calendar_organizer_email:
			self.calendar_organizer_email = frappe.db.get_value("User", self.owner_user, "email")
		if self.is_new():
			return
		url_changed = self.has_value_changed("calendar_url")
		if url_changed or self.has_value_changed("calendar_since"):
			self.calendar_sync_token = None
			self.calendar_fresh = 1
		if url_changed:
			self.calendar_url_changed = 1

	def validate_urls(self):
		server = urlparse(self.server_url or "")
		local = server.hostname in LOOPBACK and (frappe.flags.in_test or frappe.conf.developer_mode)
		if not server.hostname or not (server.scheme == "https" or (server.scheme == "http" and local)):
			frappe.throw(_("The server URL must start with https://."))
		for field in ("address_book_url", "calendar_url"):
			url = self.get(field)
			if url and not same_origin(url, self.server_url):
				frappe.throw(
					_("{0} must be on the same server as the server URL.").format(
						_(self.meta.get_label(field))
					)
				)

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
