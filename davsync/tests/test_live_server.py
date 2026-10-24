import os
import unittest
from unittest.mock import patch

import frappe
import requests
from frappe.tests import IntegrationTestCase

from davsync.caldav import sync
from davsync.carddav import pull, push
from davsync.carddav.client import CardDAVClient
from davsync.tests.helpers import make_carddav_account, make_user, tag
from davsync.tests.ical_fixtures import vevent

SERVER = os.environ.get("DAVSYNC_TEST_SERVER", "")
USER, PASSWORD = os.environ.get("DAVSYNC_TEST_USER", "test"), os.environ.get("DAVSYNC_TEST_PASSWORD", "test")
MKCOL = (
	'<?xml version="1.0"?><d:mkcol xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav">'
	"<d:set><d:prop><d:resourcetype><d:collection/><card:addressbook/></d:resourcetype>"
	"<d:displayname>{name}</d:displayname></d:prop></d:set></d:mkcol>"
)


@unittest.skipUnless(SERVER, "set DAVSYNC_TEST_SERVER to a CardDAV/CalDAV server, e.g. Radicale")
class TestLiveServer(IntegrationTestCase):
	"""Runs against a real server; the CI starts Radicale for it."""

	def setUp(self):
		self.base = SERVER.rstrip("/")
		self.http = requests.Session()
		self.http.auth = (USER, PASSWORD)
		self.book = f"{self.base}/{USER}/contacts-{tag()}/"
		self.calendar = f"{self.base}/{USER}/calendar-{tag()}/"
		self.http.request("MKCOL", self.book, data=MKCOL.format(name="Contacts")).raise_for_status()
		self.http.request("MKCALENDAR", self.calendar).raise_for_status()
		self.owner = make_user("davsync-live@example.test", roles=("System Manager",))
		self.account = make_carddav_account(
			server_url=f"{self.base}/",
			username=USER,
			app_password=PASSWORD,
			address_book_url=self.book,
			calendar_url=self.calendar,
			calendar_since="2026-01-01",
			calendar_enabled=1,
			owner_user=self.owner,
			enabled=1,
			push_enabled=1,
		).name
		for patcher in (
			patch.object(frappe.db, "commit"),
			patch.object(sync, "today", lambda: "2026-07-01"),
		):
			patcher.start()
			self.addCleanup(patcher.stop)

	def put_card(self, uid, given, family):
		text = f"BEGIN:VCARD\r\nVERSION:3.0\r\nUID:{uid}\r\nN:{family};{given};;;\r\nFN:{given} {family}\r\nEND:VCARD\r\n"
		self.http.put(
			f"{self.book}{uid}.vcf", data=text, headers={"Content-Type": "text/vcard"}
		).raise_for_status()

	def test_discovery(self):
		client = CardDAVClient(self.account)
		self.assertTrue(client.discover_address_book().startswith(f"{self.base}/{USER}/"))

	def test_two_way_contact_sync(self):
		uid = f"live-{tag()}"
		self.put_card(uid, "Live", "Server")
		self.assertEqual(pull.pull(self.account)["created"], 1)
		self.assertEqual(pull.pull(self.account)["created"], 0)
		name = frappe.db.get_value("Contact", {"carddav_account": self.account, "carddav_uid": uid})
		doc = frappe.get_doc("Contact", name)
		doc.designation = "Tester"
		doc.save()
		self.assertEqual(push.push_contact(self.account, name), "updated")
		text = self.http.get(f"{self.book}{uid}.vcf").text
		self.assertIn("TITLE:Tester", text)
		self.assertEqual(pull.pull(self.account)["updated"], 0)

	def test_calendar_import(self):
		uid = f"live-event-{tag()}"
		body = (
			"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//davsync//test//EN\r\n"
			+ "\r\n".join(
				vevent(
					uid,
					start="20260715T080000Z",
					end="20260715T090000Z",
					tzid="",
					attendees=[("guest@example.test", "Guest")],
				)
			)
			+ "\r\nEND:VCALENDAR\r\n"
		)
		self.http.put(
			f"{self.calendar}{uid}.ics", data=body, headers={"Content-Type": "text/calendar"}
		).raise_for_status()
		self.assertEqual(sync.pull(self.account)["created"], 1)
		self.assertTrue(frappe.db.exists("Event", {"caldav_account": self.account, "caldav_uid": uid}))
		self.http.delete(f"{self.calendar}{uid}.ics").raise_for_status()
		self.assertEqual(sync.pull(self.account)["cancelled"], 1)
