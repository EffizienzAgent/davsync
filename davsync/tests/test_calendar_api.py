from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from davsync.caldav import api
from davsync.caldav.client import CalDAVClient
from davsync.carddav.client import CardDAVError
from davsync.tests.helpers import make_carddav_account, make_user


class TestCalendarAccountActions(IntegrationTestCase):
	def setUp(self):
		self.account = make_carddav_account().name
		self.addCleanup(frappe.set_user, "Administrator")

	def test_discover_stores_calendar_url(self):
		with patch.object(CalDAVClient, "discover_calendar", return_value="https://x.test/work/"):
			api.discover_calendar(self.account)
		self.assertEqual(
			frappe.db.get_value("CardDAV Account", self.account, "calendar_url"), "https://x.test/work/"
		)

	def test_discover_error_is_shown(self):
		with patch.object(CalDAVClient, "discover_calendar", side_effect=CardDAVError("none")):
			with self.assertRaises(frappe.ValidationError):
				api.discover_calendar(self.account)

	def test_initial_import_is_enqueued(self):
		with patch.object(frappe, "enqueue") as enqueue:
			api.start_calendar_import(self.account)
		self.assertEqual(enqueue.call_args.args[0], "davsync.caldav.sync.run_pull")
		self.assertEqual(enqueue.call_args.kwargs["job_id"], f"carddav-calendar-import-{self.account}")
		self.assertTrue(enqueue.call_args.kwargs["initial"])

	def test_only_system_manager(self):
		frappe.set_user(make_user("calendar-desk-only@example.test", roles=["Desk User"]))
		with self.assertRaises(frappe.PermissionError):
			api.start_calendar_sync(self.account)
