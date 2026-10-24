import frappe
from frappe import _
from frappe.model.document import Document

from davsync.caldav.target import get_target


class IgnoredCalendarEvent(Document):
	def validate(self):
		if frappe.db.exists(
			"Ignored Calendar Event", {"account": self.account, "uid": self.uid, "name": ("!=", self.name)}
		):
			frappe.throw(_("This calendar event is already ignored."))

	def before_insert(self):
		get_target().check_series_permission(self.account, self.uid)

	def after_insert(self):
		get_target().set_ignored(self.account, self.uid, True)

	def on_trash(self):
		target = get_target()
		target.check_series_permission(self.account, self.uid)
		target.set_ignored(self.account, self.uid, False)
