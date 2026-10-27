from datetime import UTC, datetime
from unittest.mock import patch

import frappe
import requests
from frappe.tests import IntegrationTestCase

from davsync.caldav import client as caldav_client
from davsync.caldav import sync, writer
from davsync.caldav.client import CalDAVClient
from davsync.caldav.compose import OutgoingEvent, Person
from davsync.caldav.target import EventTarget
from davsync.carddav import client as client_module
from davsync.carddav.client import PreconditionFailed
from davsync.tests.helpers import guard_commit, make_carddav_account, make_contact, make_user, tag
from davsync.tests.test_caldav_client import calendar_entry, multistatus
from davsync.tests.test_calendar import CALENDAR, CALENDAR_PATH, FakeClient
from davsync.tests.test_carddav_client import FakeResponse, entry

HOME = "/remote.php/dav/calendars/alice/"
BUSINESS = f"{HOME}business/"


def outgoing(uid, guest, start=datetime(2026, 7, 15, 10), end=datetime(2026, 7, 15, 11)):
	return OutgoingEvent(
		uid=uid,
		summary="Kennenlernen",
		start=start,
		end=end,
		tz="Europe/Berlin",
		organizer=Person("owner@example.test", "Owner"),
		attendees=[Person(guest, "Gast")],
	)


def freebusy(*periods):
	lines = "".join(f"FREEBUSY:{p}\r\n" for p in periods)
	body = f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VFREEBUSY\r\n{lines}END:VFREEBUSY\r\nEND:VCALENDAR\r\n"
	return FakeResponse(body, 200)


class TestCalendarClientWrite(IntegrationTestCase):
	def setUp(self):
		account = make_carddav_account(calendar_url=f"https://cloud.example.test{BUSINESS}")
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

	def test_put_event_uses_calendar_content_type_and_preconditions(self):
		with self.patched([FakeResponse("", 201, {"ETag": '"n1"'})]) as request:
			self.assertEqual(self.client.put_event(f"{BUSINESS}a.ics", "x", None), '"n1"')
		headers = request.call_args.kwargs["headers"]
		self.assertEqual(headers["Content-Type"], "text/calendar; charset=utf-8")
		self.assertEqual(headers["If-None-Match"], "*")
		with self.patched([FakeResponse("", 204, {"ETag": '"n2"'})]) as request:
			self.client.put_event(f"{BUSINESS}a.ics", "x", '"n1"')
		self.assertEqual(request.call_args.kwargs["headers"]["If-Match"], '"n1"')
		with self.patched([FakeResponse("", 412)]), self.assertRaises(PreconditionFailed):
			self.client.put_event(f"{BUSINESS}a.ics", "x", '"old"')

	def test_delete_event_sends_if_match(self):
		with self.patched([FakeResponse("", 204)]) as request:
			self.client.delete_event(f"{BUSINESS}a.ics", '"n1"')
		self.assertEqual(request.call_args.args[1], "DELETE")
		self.assertEqual(request.call_args.kwargs["headers"]["If-Match"], '"n1"')

	def test_supports_scheduling_reads_dav_header(self):
		with self.patched(
			[FakeResponse("", 200, {"DAV": "1, calendar-access, calendar-auto-schedule"})]
		) as r:
			self.assertTrue(self.client.supports_scheduling())
		self.assertEqual(r.call_args.args[1:3], ("OPTIONS", self.client.calendar_url))
		with self.patched([FakeResponse("", 200, {"DAV": "1, calendar-access"})]):
			self.assertFalse(self.client.supports_scheduling())

	def home_responses(self, *names):
		principal = entry(
			"/remote.php/dav/",
			"<d:current-user-principal><d:href>/remote.php/dav/principals/users/alice/</d:href>"
			"</d:current-user-principal>",
		)
		home = entry(
			"/remote.php/dav/principals/users/alice/",
			f"<cal:calendar-home-set><d:href>{HOME}</d:href></cal:calendar-home-set>",
		)
		listing = multistatus(
			*(calendar_entry(name, name) for name in names),
			entry(f"{HOME}inbox/", "<d:resourcetype><d:collection/></d:resourcetype>"),
		)
		return [multistatus(principal), multistatus(home), listing]

	def test_free_busy_merges_calendars(self):
		first = freebusy("20261027T080000Z/20261027T090000Z", "20261027T140000Z/PT1H")
		second = freebusy("20261027T084500Z/20261027T093000Z")
		start, end = datetime(2026, 10, 27, tzinfo=UTC), datetime(2026, 10, 28, tzinfo=UTC)
		with self.patched([*self.home_responses("personal", "business"), first, second]) as request:
			result = self.client.free_busy(start, end)
		self.assertEqual(
			result,
			[
				(datetime(2026, 10, 27, 8, tzinfo=UTC), datetime(2026, 10, 27, 9, 30, tzinfo=UTC)),
				(datetime(2026, 10, 27, 14, tzinfo=UTC), datetime(2026, 10, 27, 15, tzinfo=UTC)),
			],
		)
		report = request.call_args_list[3]
		self.assertEqual(report.args[1], "REPORT")
		self.assertIn("free-busy-query", report.kwargs["data"].decode())
		self.assertIn('start="20261027T000000Z"', report.kwargs["data"].decode())

	def test_free_busy_skips_failing_calendar(self):
		ok = freebusy("20261027T080000Z/20261027T090000Z")
		start, end = datetime(2026, 10, 27, tzinfo=UTC), datetime(2026, 10, 28, tzinfo=UTC)
		with (
			self.patched([*self.home_responses("broken", "business"), FakeResponse("", 403), ok]),
			patch.object(caldav_client, "LOGGED_FAILURES", set()),
			patch.object(frappe, "log_error") as log,
		):
			result = self.client.free_busy(start, end)
		self.assertEqual(
			result, [(datetime(2026, 10, 27, 8, tzinfo=UTC), datetime(2026, 10, 27, 9, tzinfo=UTC))]
		)
		log.assert_called_once()


class FakeWriteClient:
	conflict = False

	def __init__(self, account):
		self.account = account

	def put_event(self, href, text, etag):
		if FakeWriteClient.conflict:
			raise PreconditionFailed("412")
		new = f'"{tag()}"'
		FakeClient.objects[href] = (new, text)
		return new

	def delete_event(self, href, etag):
		FakeClient.objects.pop(href, None)


class TestCalendarWriter(IntegrationTestCase):
	def setUp(self):
		self.owner = make_user("davsync-writer@example.test")
		self.account = make_carddav_account(
			owner_user=self.owner,
			calendar_url=CALENDAR,
			calendar_enabled=1,
			calendar_write_enabled=1,
			calendar_since="2026-01-01",
		).name
		guard_commit(self)
		FakeClient.objects, FakeClient.deleted, FakeClient.full = {}, [], True
		FakeWriteClient.conflict = False
		for module, attr, value in (
			(sync, "CalDAVClient", FakeClient),
			(writer, "CalDAVClient", FakeWriteClient),
			(sync, "get_system_timezone", lambda: "Europe/Berlin"),
			(sync, "today", lambda: "2026-07-01"),
		):
			patcher = patch.object(module, attr, value)
			patcher.start()
			self.addCleanup(patcher.stop)

	def events(self):
		return frappe.get_all(
			"Event", filters={"caldav_account": self.account}, fields=["name", "caldav_href"]
		)

	def test_write_event_imports_own_object_without_duplicates(self):
		guest = f"guest-{tag()}@example.test"
		make_contact("Gast", "Teilnehmer", emails=[guest])
		uid = f"u-{tag()}"
		href, _etag = writer.write_event(self.account, outgoing(uid, guest))
		self.assertEqual(href, f"{CALENDAR_PATH}{uid}.ics")
		self.assertEqual(len(self.events()), 1)
		before = self.events()
		counts = sync.pull(self.account)
		self.assertEqual((counts["skipped"], counts["created"], counts["updated"]), (1, 0, 0))
		self.assertEqual(self.events(), before)
		keys = frappe.get_all("Event", filters={"caldav_account": self.account}, pluck="caldav_uid")
		self.assertEqual(keys, [uid])

	def test_failed_import_raises_and_next_sync_retries(self):
		guest = f"guest-{tag()}@example.test"
		make_contact("Gast", "Teilnehmer", emails=[guest])
		with patch.object(EventTarget, "upsert", side_effect=RuntimeError("boom")):
			with self.assertRaises(writer.ImportFailed) as caught:
				writer.write_event(self.account, outgoing(f"u-{tag()}", guest))
		href, etag = caught.exception.href, caught.exception.etag
		row = frappe.db.get_value(
			"Calendar Object", {"account": self.account, "href": href}, ["etag", "failed"], as_dict=True
		)
		self.assertEqual((row.etag, row.failed), (etag, 1))
		self.assertEqual(self.events(), [])
		self.assertEqual(sync.pull(self.account)["created"], 1)
		self.assertEqual(len(self.events()), 1)

	def test_write_requires_opt_in(self):
		frappe.db.set_value("CardDAV Account", self.account, "calendar_write_enabled", 0)
		with self.assertRaises(frappe.ValidationError):
			writer.write_event(self.account, outgoing("u-1", "g@example.test"))
		with self.assertRaises(frappe.ValidationError):
			writer.remove_event(self.account, f"{CALENDAR_PATH}u-1.ics", '"e"')

	def test_conflict_refetches_and_raises(self):
		FakeWriteClient.conflict = True
		href = f"{CALENDAR_PATH}u-2.ics"
		with (
			patch.object(writer, "refetch") as refetch,
			self.assertRaises(PreconditionFailed),
		):
			writer.write_event(self.account, outgoing("u-2", "g@example.test"), href, '"old"')
		refetch.assert_called_once_with(self.account, [href])

	def test_remove_event_cancels_and_forgets(self):
		guest = f"guest-{tag()}@example.test"
		make_contact("Gast", "Teilnehmer", emails=[guest])
		href, etag = writer.write_event(self.account, outgoing(f"u-{tag()}", guest))
		writer.remove_event(self.account, href, etag)
		self.assertFalse(frappe.db.exists("Calendar Object", {"account": self.account, "href": href}))
		self.assertEqual(
			frappe.get_all("Event", filters={"caldav_account": self.account, "status": "Open"}), []
		)

	def test_scheduling_is_cached(self):
		frappe.cache.delete_value(f"davsync:scheduling:{self.account}")
		with patch.object(FakeWriteClient, "supports_scheduling", create=True, return_value=True) as probe:
			self.assertTrue(writer.scheduling(self.account))
			self.assertTrue(writer.scheduling(self.account))
		self.assertEqual(probe.call_count, 1)
		frappe.cache.delete_value(f"davsync:scheduling:{self.account}")
