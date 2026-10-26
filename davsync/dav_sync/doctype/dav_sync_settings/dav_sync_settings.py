from frappe.model.document import Document

from davsync.settings import DEFAULT_INTERVAL


class DAVSyncSettings(Document):
	def validate(self):
		for fieldname in ("contacts_interval", "calendar_interval"):
			if not self.get(fieldname) or self.get(fieldname) < 1:
				self.set(fieldname, DEFAULT_INTERVAL)
