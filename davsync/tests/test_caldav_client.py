from unittest.mock import patch

import requests
from frappe.tests import IntegrationTestCase

from davsync.caldav import client as caldav_client
from davsync.caldav.client import CalDAVClient
from davsync.carddav import client as client_module
from davsync.carddav.client import CardDAVError
from davsync.tests.helpers import guard_commit, make_carddav_account
from davsync.tests.test_carddav_client import FakeResponse, entry

CALENDAR = "https://cloud.example.test/remote.php/dav/calendars/alice/business/"
NS = 'xmlns:d="DAV:" xmlns:cal="urn:ietf:params:xml:ns:caldav"'


def multistatus(*responses, token=None):
	tail = f"<d:sync-token>{token}</d:sync-token>" if token else ""
	return FakeResponse(
		f'<?xml version="1.0"?><d:multistatus {NS}>{"".join(responses)}{tail}</d:multistatus>'
	)


def calendar_entry(name, display):
	return entry(
		f"/remote.php/dav/calendars/alice/{name}/",
		f"<d:displayname>{display}</d:displayname>"
		"<d:resourcetype><d:collection/><cal:calendar/></d:resourcetype>",
	)


class TestCalDAVClient(IntegrationTestCase):
	def setUp(self):
		account = make_carddav_account(calendar_url=CALENDAR, calendar_since="2026-07-01")
		self.client = CalDAVClient(account.name)
		guard_commit(self)
		for module, attr, value in (
			(client_module, "sleep", None),
			(caldav_client, "get_system_timezone", lambda: "Europe/Berlin"),
		):
			patcher = patch.object(module, attr, value) if value else patch.object(module, attr)
			patcher.start()
			self.addCleanup(patcher.stop)

	def patched(self, responses):
		queue = list(responses)
		return patch.object(
			requests.Session, "request", autospec=True, side_effect=lambda *a, **k: queue.pop(0)
		)

	def test_discover_finds_named_calendar(self):
		principal = entry(
			"/remote.php/dav/",
			"<d:current-user-principal><d:href>/remote.php/dav/principals/users/alice/</d:href>"
			"</d:current-user-principal>",
		)
		home = entry(
			"/remote.php/dav/principals/users/alice/",
			"<cal:calendar-home-set><d:href>/remote.php/dav/calendars/alice/</d:href></cal:calendar-home-set>",
		)
		listing = multistatus(
			calendar_entry("personal", "Personal"),
			calendar_entry("business", "Business"),
			entry(
				"/remote.php/dav/calendars/alice/inbox/", "<d:resourcetype><d:collection/></d:resourcetype>"
			),
		)
		with self.patched([multistatus(principal), multistatus(home), listing]):
			self.assertEqual(self.client.discover_calendar("Business"), CALENDAR)
		with self.patched([multistatus(principal), multistatus(home), listing]):
			self.assertEqual(self.client.discover_calendar(), CALENDAR.replace("business", "personal"))

	def test_discover_without_named_calendar_fails(self):
		principal = entry(
			"/remote.php/dav/",
			"<d:current-user-principal><d:href>/p/</d:href></d:current-user-principal>",
		)
		home = entry("/p/", "<cal:calendar-home-set><d:href>/c/</d:href></cal:calendar-home-set>")
		listing = multistatus(calendar_entry("personal", "Personal"))
		with self.patched([multistatus(principal), multistatus(home), listing]):
			with self.assertRaises(CardDAVError):
				self.client.discover_calendar("business")

	def test_full_listing_uses_time_range_and_token(self):
		token = multistatus(
			entry("/remote.php/dav/calendars/alice/business/", "<d:sync-token>tok-9</d:sync-token>")
		)
		listing = multistatus(
			entry("/remote.php/dav/calendars/alice/business/1.ics", '<d:getetag>"e1"</d:getetag>')
		)
		with self.patched([token, listing]) as request:
			changed, deleted, new_token, full = self.client.sync(None)
		self.assertEqual(
			(changed, deleted, new_token, full),
			([("/remote.php/dav/calendars/alice/business/1.ics", '"e1"')], [], "tok-9", True),
		)
		method, url = request.call_args_list[1].args[1:3]
		body = request.call_args_list[1].kwargs["data"].decode()
		self.assertEqual((method, url), ("REPORT", CALENDAR))
		self.assertIn('start="20260630T220000Z"', body)
		self.assertIn("cal:calendar-query", body)

	def test_sync_collection_with_token(self):
		response = multistatus(
			entry("/remote.php/dav/calendars/alice/business/1.ics", '<d:getetag>"e2"</d:getetag>'),
			"<d:response><d:href>/remote.php/dav/calendars/alice/business/2.ics</d:href><d:status>HTTP/1.1 404 Not Found</d:status></d:response>",
			token="tok-10",
		)
		with self.patched([response]):
			self.assertEqual(
				self.client.sync("tok-9"),
				(
					[("/remote.php/dav/calendars/alice/business/1.ics", '"e2"')],
					["/remote.php/dav/calendars/alice/business/2.ics"],
					"tok-10",
					False,
				),
			)

	def test_multiget_returns_calendar_data(self):
		response = multistatus(
			entry(
				"/remote.php/dav/calendars/alice/business/1.ics",
				'<d:getetag>"e1"</d:getetag><cal:calendar-data>BEGIN:VCALENDAR</cal:calendar-data>',
			)
		)
		with self.patched([response]) as request:
			self.assertEqual(
				self.client.multiget(["/remote.php/dav/calendars/alice/business/1.ics"]),
				[("/remote.php/dav/calendars/alice/business/1.ics", '"e1"', "BEGIN:VCALENDAR")],
			)
		self.assertIn("cal:calendar-multiget", request.call_args.kwargs["data"].decode())

	def test_calendar_is_read_only(self):
		with self.assertRaises(CardDAVError):
			self.client.put("/remote.php/dav/calendars/alice/business/1.ics", "x", None)
		with self.assertRaises(CardDAVError):
			self.client.delete("/remote.php/dav/calendars/alice/business/1.ics", None)
