from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from davsync.carddav.api import discover
from davsync.carddav.client import CardDAVClient
from davsync.tests.helpers import guard_commit, make_carddav_account, make_user


class TestCardDAVAccount(IntegrationTestCase):
	def setUp(self):
		guard_commit(self)

	def test_push_enabled_by_default_and_sync_state_empty(self):
		account = make_carddav_account()
		self.assertEqual(account.push_enabled, 1)
		self.assertFalse(account.sync_token)

	def test_password_masked_for_system_manager_client_get(self):
		account = make_carddav_account(app_password="super-secret")
		with self.set_user("Administrator"):
			result = frappe.client.get("CardDAV Account", account.name)
		self.assertNotIn("super-secret", str(result))
		self.assertEqual(account.get_password("app_password"), "super-secret")

	def test_only_system_manager_has_access(self):
		account = make_carddav_account()
		for role in ("Desk User",):
			user = make_user(f"cd-{role.lower().replace(' ', '-')}@example.com", [role])
			with self.set_user(user):
				self.assertFalse(frappe.has_permission("CardDAV Account", "read", account))

	def test_discover_stores_address_book_url(self):
		account = make_carddav_account()
		with patch.object(CardDAVClient, "discover_address_book", return_value="https://x.test/book/"):
			discover(account.name)
		self.assertEqual(
			frappe.db.get_value("CardDAV Account", account.name, "address_book_url"), "https://x.test/book/"
		)

	def test_discover_requires_system_manager(self):
		account = make_carddav_account()
		user = make_user("cd-discover@example.com", ["Desk User"])
		with self.set_user(user), self.assertRaises(frappe.PermissionError):
			discover(account.name)

	def test_ignored_attendee_patterns_are_validated(self):
		account = make_carddav_account()
		account.append("ignored_attendees", {"pattern": " *@Example.TEST "})
		account.save()
		self.assertEqual(account.ignored_attendees[0].pattern, "*@example.test")
		account.append("ignored_attendees", {"pattern": "no-address"})
		self.assertRaises(frappe.ValidationError, account.save)


class TestSettings(IntegrationTestCase):
	def tearDown(self):
		frappe.get_single("DAV Sync Settings").update({"default_country": None, "phone_region": ""}).save()

	def test_defaults_fall_back_to_system_country(self):
		from davsync import settings

		with patch.object(frappe.db, "get_default", return_value="Austria"):
			self.assertEqual(settings.default_country(), "Austria")
			self.assertEqual(settings.phone_region(), "AT")
		frappe.get_single("DAV Sync Settings").update(
			{"default_country": "Switzerland", "phone_region": "de"}
		).save()
		self.assertEqual((settings.default_country(), settings.phone_region()), ("Switzerland", "DE"))
