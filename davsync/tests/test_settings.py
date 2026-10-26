import time
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from davsync import settings
from davsync.caldav import sync
from davsync.carddav import pull
from davsync.tests.helpers import make_carddav_account


class TestSyncInterval(IntegrationTestCase):
	def setUp(self):
		for kind in ("contacts", "calendar"):
			self.addCleanup(frappe.cache.delete_value, f"davsync-last-run:{kind}")
			frappe.cache.delete_value(f"davsync-last-run:{kind}")
		self.addCleanup(self.configure, contacts_interval=5, calendar_interval=5)

	def configure(self, **values):
		frappe.get_single("DAV Sync Settings").update(values).save()

	def test_empty_interval_falls_back_to_default(self):
		self.configure(contacts_interval=0, calendar_interval=None)
		doc = frappe.get_single("DAV Sync Settings")
		self.assertEqual((doc.contacts_interval, doc.calendar_interval), (5, 5))

	def test_due_once_per_interval(self):
		self.configure(calendar_interval=2)
		self.assertTrue(settings.sync_due("calendar"))
		self.assertFalse(settings.sync_due("calendar"))
		frappe.cache.set_value("davsync-last-run:calendar", time.time() - 100)
		self.assertTrue(settings.sync_due("calendar"))

	def test_kinds_are_independent(self):
		self.assertTrue(settings.sync_due("contacts"))
		self.assertTrue(settings.sync_due("calendar"))

	def test_scheduled_jobs_skip_until_due(self):
		settings.sync_due("contacts")
		settings.sync_due("calendar")
		with (
			patch.object(pull, "enabled_accounts") as contacts,
			patch.object(sync, "calendar_accounts") as calendars,
		):
			pull.pull_all()
			sync.sync_all()
		contacts.assert_not_called()
		calendars.assert_not_called()

	def test_refetch_note_names_interval(self):
		self.configure(calendar_interval=2)
		account = make_carddav_account().name
		frappe.db.set_value("CardDAV Account", account, "calendar_enabled", 1)
		self.assertIn("2 min", sync.refetch_note(account))
