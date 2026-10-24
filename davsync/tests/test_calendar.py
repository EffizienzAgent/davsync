from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from davsync.caldav import sync
from davsync.caldav.target import CalendarTarget
from davsync.tests.helpers import guard_commit, make_carddav_account, make_contact, make_user, tag, use_hooks
from davsync.tests.ical_fixtures import calendar, vevent

CALENDAR = "https://cloud.example.test/remote.php/dav/calendars/alice/work/"
CALENDAR_PATH = "/remote.php/dav/calendars/alice/work/"


class FakeClient:
	objects: dict = {}  # noqa: RUF012
	deleted: list = []  # noqa: RUF012
	full = True

	def __init__(self, account):
		self.account = account

	def sync(self, token):
		changed = [(href, etag) for href, (etag, _text) in FakeClient.objects.items()]
		return changed, list(FakeClient.deleted), "cal-token", FakeClient.full

	def multiget(self, hrefs):
		return [(h, *FakeClient.objects[h]) for h in hrefs if h in FakeClient.objects]


class Recorder(CalendarTarget):
	upserts: list = []  # noqa: RUF012

	def existing(self, account, href):
		return {}

	def find(self, account, key):
		return None

	def upsert(self, context, href, etag, event, people, name):
		Recorder.upserts.append((event.key, [p.email for p in people or []]))
		return "created"

	def cancel(self, name):
		return 0


class TestCalendarSync(IntegrationTestCase):
	def setUp(self):
		self.owner = make_user("davsync-calendar@example.test")
		self.account = make_carddav_account(
			owner_user=self.owner, calendar_url=CALENDAR, calendar_enabled=1, calendar_since="2026-01-01"
		).name
		guard_commit(self)
		FakeClient.objects, FakeClient.deleted, FakeClient.full = {}, [], True
		Recorder.upserts = []
		for module, attr, value in (
			(sync, "CalDAVClient", FakeClient),
			(sync, "get_system_timezone", lambda: "Europe/Berlin"),
			(sync, "today", lambda: "2026-07-01"),
		):
			patcher = patch.object(module, attr, value)
			patcher.start()
			self.addCleanup(patcher.stop)

	def add(self, *events):
		href = f"{CALENDAR_PATH}{tag()}.ics"
		FakeClient.objects[href] = (f'"{tag()}"', calendar(*events))
		return href

	def pull(self):
		return sync.pull(self.account)

	def events(self, href):
		return frappe.get_all(
			"Event",
			filters={"caldav_account": self.account, "caldav_href": href},
			fields=["name", "subject", "status", "starts_on", "owner", "event_type"],
		)

	def test_event_with_participants_is_imported_privately(self):
		email = f"guest-{tag()}@example.test"
		contact = make_contact("Gast", "Teilnehmer", emails=[email])
		href = self.add(vevent(f"u-{tag()}", attendees=[(email, "Gast")]))
		self.assertEqual(self.pull()["created"], 1)
		[event] = self.events(href)
		self.assertEqual((event.subject, event.status, event.event_type), ("Abstimmung", "Open", "Private"))
		self.assertEqual(event.owner, self.owner)
		participants = frappe.get_all(
			"Event Participants", filters={"parent": event.name}, pluck="reference_docname"
		)
		self.assertEqual(participants, [contact.name])
		self.assertEqual(self.pull()["skipped"], 1)

	def test_event_without_participants_is_skipped(self):
		href = self.add(vevent(f"u-{tag()}"))
		self.assertEqual(self.pull()["skipped"], 1)
		self.assertEqual(self.events(href), [])

	def test_own_addresses_from_settings_are_no_participants(self):
		frappe.get_single("DAV Sync Settings").update({"self_emails": "me@example.test"}).save()
		self.addCleanup(lambda: frappe.get_single("DAV Sync Settings").update({"self_emails": ""}).save())
		href = self.add(vevent(f"u-{tag()}", attendees=[("me@example.test", "Me")]))
		self.pull()
		self.assertEqual(self.events(href), [])

	def test_ignored_attendee_pattern(self):
		account = frappe.get_doc("CardDAV Account", self.account)
		account.append("ignored_attendees", {"pattern": "*@newsletter.example.test"})
		account.save()
		href = self.add(vevent(f"u-{tag()}", attendees=[("news@newsletter.example.test", "News")]))
		self.pull()
		self.assertEqual(self.events(href), [])

	def test_deleted_object_cancels_event(self):
		href = self.add(vevent(f"u-{tag()}", attendees=[(f"x-{tag()}@example.test", "X")]))
		self.pull()
		FakeClient.objects.pop(href)
		FakeClient.full, FakeClient.deleted = False, [href]
		self.assertEqual(self.pull()["cancelled"], 1)
		self.assertEqual(self.events(href)[0].status, "Cancelled")

	def test_recurring_series_expands_and_can_be_ignored(self):
		uid = f"series-{tag()}"
		href = self.add(
			vevent(
				uid,
				start="20260706T100000",
				end="20260706T110000",
				attendees=[(f"s-{tag()}@example.test", "S")],
				extra=["RRULE:FREQ=WEEKLY;COUNT=3"],
			)
		)
		self.assertEqual(self.pull()["created"], 3)
		ignored = frappe.get_doc(
			{"doctype": "Ignored Calendar Event", "account": self.account, "uid": uid, "title": "S"}
		).insert()
		self.assertEqual({e.status for e in self.events(href)}, {"Cancelled"})
		ignored.delete()
		self.assertIsNone(
			frappe.db.get_value("Calendar Object", {"account": self.account, "href": href}, "etag")
		)
		self.pull()
		self.assertEqual({e.status for e in self.events(href)}, {"Open"})

	def test_mass_cancel_is_blocked(self):
		for _ in range(25):
			self.add(vevent(f"u-{tag()}", attendees=[(f"m-{tag()}@example.test", "M")]))
		self.pull()
		FakeClient.objects = {}
		self.pull()
		self.assertIn("skipped", frappe.db.get_value("CardDAV Account", self.account, "calendar_last_error"))
		self.assertEqual(frappe.db.count("Event", {"caldav_account": self.account, "status": "Cancelled"}), 0)

	def test_target_hook_receives_events(self):
		use_hooks(self, davsync_calendar_target=["davsync.tests.test_calendar.Recorder"])
		uid = f"u-{tag()}"
		self.add(vevent(uid, attendees=[("hook@example.test", "Hook")]))
		self.pull()
		self.assertEqual(Recorder.upserts, [(uid, ["hook@example.test"])])

	def test_changing_calendar_url_forces_full_listing(self):
		account = frappe.get_doc("CardDAV Account", self.account)
		account.calendar_sync_token = "old"
		account.calendar_url = CALENDAR.replace("work", "other")
		account.save()
		account.reload()
		self.assertEqual((account.calendar_fresh, account.calendar_url_changed), (1, 1))
		self.assertFalse(account.calendar_sync_token)


class TestEventUidIndex(IntegrationTestCase):
	def make_event(self, account, uid):
		return frappe.get_doc(
			{
				"doctype": "Event",
				"subject": "Index",
				"starts_on": "2026-07-15 10:00:00",
				"caldav_account": account,
				"caldav_uid": uid,
			}
		).insert()

	def test_uid_is_unique_per_account_only(self):
		first, second = make_carddav_account().name, make_carddav_account().name
		uid = f"shared-{tag()}"
		self.make_event(first, uid)
		self.make_event(second, uid)
		with self.assertRaises(frappe.UniqueValidationError):
			self.make_event(first, uid)
