import time
from unittest.mock import patch

import frappe
import requests
from frappe.tests import IntegrationTestCase

from davsync.caldav import ical, sync
from davsync.carddav import client as client_module
from davsync.carddav import pull, push
from davsync.carddav.client import CardDAVClient, CardDAVError, UnsafeURL, member_url
from davsync.tests.helpers import (
	guard_commit,
	make_carddav_account,
	make_contact,
	make_user,
)
from davsync.tests.ical_fixtures import calendar, vevent
from davsync.tests.test_carddav_client import FakeResponse, entry, multistatus
from davsync.tests.test_contacts import BOOK, BOOK_PATH, ContactSyncCase, FakeServer

ATTACKER = "davsync-attacker@example.test"
FORGED = {
	"carddav_account": None,
	"carddav_uid": "forged-uid",
	"carddav_href": "https://evil.example.test/steal",
	"carddav_etag": '"forged"',
	"carddav_raw": "BEGIN:VCARD\r\nEND:VCARD\r\n",
}


class TestForgedFields(ContactSyncCase):
	def setUp(self):
		super().setUp()
		make_user(ATTACKER, roles=("System Manager",))
		self.addCleanup(frappe.set_user, "Administrator")

	def test_forged_fields_on_insert_are_dropped(self):
		frappe.set_user(ATTACKER)
		doc = make_contact("Forged", "Insert", **{**FORGED, "carddav_account": self.account})
		values = frappe.db.get_value("Contact", doc.name, list(FORGED), as_dict=True)
		self.assertEqual(set(values.values()), {None})

	def test_forged_fields_on_update_are_restored(self):
		href = self.card("Real", "Card")
		self.pull()
		doc = self.contact_at(href)
		frappe.set_user(ATTACKER)
		doc.update({**FORGED, "carddav_account": self.account, "carddav_href": f"{BOOK_PATH}../"})
		doc.save()
		stored = frappe.db.get_value("Contact", doc.name, ["carddav_href", "carddav_etag", "carddav_uid"])
		self.assertEqual(stored[0], href)
		self.assertNotEqual(stored[1], '"forged"')
		self.assertNotEqual(stored[2], "forged-uid")

	def test_forged_event_fields_are_dropped(self):
		frappe.set_user(ATTACKER)
		event = frappe.get_doc(
			{
				"doctype": "Event",
				"subject": "Forged",
				"starts_on": "2026-07-15 10:00:00",
				"caldav_account": self.account,
				"caldav_href": "https://evil.example.test/",
			}
		).insert()
		self.assertIsNone(frappe.db.get_value("Event", event.name, "caldav_href"))

	def test_archiving_never_deletes_outside_the_book(self):
		href = self.card("Archive", "Me")
		self.pull()
		doc = self.contact_at(href)
		frappe.db.set_value("Contact", doc.name, "carddav_href", "../", update_modified=False)
		doc.reload()
		doc.archived = 1
		doc.save()
		real = CardDAVClient.__new__(CardDAVClient)
		real.address_book_url = BOOK
		with self.assertRaises(UnsafeURL):
			push.delete_card(real, self.account, doc.carddav_href, doc.carddav_etag)

	def test_delete_conflict_keeps_the_card_and_does_not_retry(self):
		href = self.card("Changed", "Remotely")
		self.pull()
		doc = self.contact_at(href)
		FakeServer.cards[href] = ('"remote-new"', FakeServer.cards[href][1])

		def delete(server, target, etag):
			FakeServer.calls.append(("delete", target, etag))
			raise client_module.PreconditionFailed("412")

		doc.archived = 1
		doc.save()
		with patch.object(FakeServer, "delete", delete), patch.object(frappe.db, "commit"):
			self.assertEqual(push.push_contact(self.account, doc.name), "conflict")
		self.assertEqual(
			[c for c in FakeServer.calls if c[0] == "delete"], [("delete", href, doc.carddav_etag)]
		)
		self.assertIn(href, FakeServer.cards)
		self.assertEqual(frappe.db.get_value("Contact", doc.name, "carddav_etag"), '"remote-new"')

	def test_mass_delete_switches_push_off(self):
		key = frappe.cache.make_key(f"carddav-deletes:{self.account}")
		frappe.cache.delete(key)
		self.addCleanup(frappe.cache.delete, key)
		client = FakeServer(self.account)
		for i in range(push.MASS_DELETE_COUNT):
			push.delete_card(client, self.account, f"{BOOK_PATH}{i}.vcf", '"e"')
		with self.assertRaises(push.MassDeleteBlocked):
			push.delete_card(client, self.account, f"{BOOK_PATH}last.vcf", '"e"')
		self.assertEqual(frappe.db.get_value("CardDAV Account", self.account, "push_enabled"), 0)
		self.assertEqual(len([c for c in FakeServer.calls if c[0] == "delete"]), push.MASS_DELETE_COUNT)

	def test_remote_address_and_notes_are_sanitized(self):
		payload = '<img src=x onerror="alert(1)">'
		href = self.card("Xss", "Card", "ADR;TYPE=WORK:;;Weg 1;Lübeck;Nord;23552;Germany")
		self.pull()
		text = FakeServer.cards[href][1]
		FakeServer.cards[href] = ('"e2"', text.replace(";;Weg 1;Lübeck;Nord;", f";;Weg 1;Lübeck;{payload};"))
		self.pull()
		state = frappe.db.get_value(
			"Address",
			{
				"name": (
					"in",
					frappe.get_all(
						"Dynamic Link",
						{"parenttype": "Address", "link_name": self.contact_at(href).name},
						pluck="parent",
					),
				)
			},
			"state",
		)
		self.assertNotIn("onerror", state or "")
		doc = self.contact_at(href)
		remote = pull.vcard.parse(text.replace("END:VCARD", f"NOTE:{payload}\r\nEND:VCARD"))
		push.take_remote_only(doc, remote)
		self.assertNotIn("onerror", frappe.db.get_value("Contact", doc.name, "notes") or "")


class TestClientBoundaries(IntegrationTestCase):
	def setUp(self):
		guard_commit(self)
		self.account = make_carddav_account(address_book_url=BOOK).name
		self.client = CardDAVClient(self.account)

	def test_member_url_accepts_cards_in_the_book(self):
		self.assertEqual(member_url(BOOK, f"{BOOK_PATH}a.vcf"), f"{BOOK}a.vcf")
		self.assertEqual(member_url(BOOK, "a%20b.vcf"), f"{BOOK}a%20b.vcf")

	def test_member_url_rejects_everything_else(self):
		for href in (
			"https://evil.example.test/remote.php/dav/addressbooks/users/alice/contacts/a.vcf",
			"http://cloud.example.test/remote.php/dav/addressbooks/users/alice/contacts/a.vcf",
			"https://cloud.example.test:8443/remote.php/dav/addressbooks/users/alice/contacts/a.vcf",
			"../",
			"../other/a.vcf",
			f"{BOOK_PATH}../x.vcf",
			f"{BOOK_PATH}%2e%2e/x.vcf",
			f"{BOOK_PATH}a/..%2f..%2fx.vcf",
			BOOK_PATH,
			BOOK,
			f"{BOOK_PATH}.",
			f"{BOOK_PATH}a.vcf?x=1",
			"//evil.example.test/a.vcf",
		):
			with self.subTest(href=href), self.assertRaises(UnsafeURL):
				member_url(BOOK, href)

	def test_forged_href_never_reaches_the_network(self):
		with patch.object(requests.Session, "request") as request:
			for call in (
				lambda: self.client.put("https://evil.example.test/a.vcf", "x", None),
				lambda: self.client.delete("../", '"e"'),
				lambda: self.client.delete(BOOK, '"e"'),
			):
				with self.assertRaises(UnsafeURL):
					call()
			self.assertEqual(self.client.multiget(["https://evil.example.test/a.vcf"]), [])
		request.assert_not_called()

	def test_delete_requires_an_etag(self):
		with patch.object(requests.Session, "request") as request:
			with self.assertRaises(CardDAVError):
				self.client.delete(f"{BOOK_PATH}a.vcf", None)
		request.assert_not_called()

	def test_principal_on_another_host_is_refused(self):
		principal = entry(
			"/",
			"<d:current-user-principal><d:href>https://evil.example.test/p/</d:href></d:current-user-principal>",
		)
		with patch.object(requests.Session, "request", return_value=multistatus(principal)) as request:
			with self.assertRaises(UnsafeURL):
				self.client.discover_address_book()
		self.assertEqual(request.call_count, 1)

	def test_redirects_are_not_followed(self):
		redirect = FakeResponse("", 301, {"Location": "https://evil.example.test/"})
		with patch.object(requests.Session, "request", return_value=redirect) as request:
			with self.assertRaisesRegex(CardDAVError, "redirected"):
				self.client.sync(None)
		self.assertFalse(request.call_args.kwargs["allow_redirects"])

	def test_oversized_response_is_refused(self):
		with patch.object(client_module, "MAX_RESPONSE_BYTES", 10):
			with patch.object(requests.Session, "request", return_value=FakeResponse("x" * 100)):
				with self.assertRaisesRegex(CardDAVError, "too large"):
					self.client.sync(None)

	def test_xml_entity_expansion_is_refused(self):
		bomb = '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaa">]><d:multistatus xmlns:d="DAV:">&a;</d:multistatus>'
		with patch.object(requests.Session, "request", return_value=FakeResponse(bomb)):
			with self.assertRaisesRegex(CardDAVError, "invalid XML"):
				self.client.sync(None)


class TestAccountUrls(IntegrationTestCase):
	def test_server_url_must_use_https(self):
		with self.assertRaises(frappe.ValidationError):
			make_carddav_account(server_url="http://cloud.example.test/remote.php/dav")
		make_carddav_account(server_url="http://127.0.0.1:5232/")

	def test_collections_must_be_on_the_server(self):
		for field in ("address_book_url", "calendar_url"):
			with self.subTest(field=field), self.assertRaises(frappe.ValidationError):
				make_carddav_account(**{field: "https://evil.example.test/book/"})


class TestRecurrenceBombs(IntegrationTestCase):
	tz = __import__("zoneinfo").ZoneInfo("Europe/Berlin")
	since = __import__("datetime").datetime(2026, 7, 1)
	until = __import__("datetime").datetime(2026, 10, 1)

	def parse(self, *extra, start="20260706T100000"):
		return ical.parse(
			calendar(vevent("bomb", start=start, end=None, extra=extra)), self.tz, self.since, self.until
		)

	def test_sub_daily_rules_are_refused(self):
		for rule in ("FREQ=SECONDLY", "FREQ=MINUTELY;INTERVAL=1", "FREQ=HOURLY", "INTERVAL=2"):
			with self.subTest(rule=rule), self.assertRaises(ical.UnsupportedEvent):
				self.parse(f"RRULE:{rule}")

	def test_long_past_series_is_refused_quickly(self):
		started = time.monotonic()
		with self.assertRaises(ical.UnsupportedEvent):
			self.parse("RRULE:FREQ=DAILY", start="17000101T100000")
		self.assertLess(time.monotonic() - started, 5)

	def test_too_many_rules_are_refused(self):
		with self.assertRaises(ical.UnsupportedEvent):
			self.parse(*(f"RRULE:FREQ=WEEKLY;BYDAY={d}" for d in ("MO", "TU", "WE", "TH", "FR", "SA")))

	def test_normal_series_still_expand(self):
		self.assertEqual(len(self.parse("RRULE:FREQ=WEEKLY;COUNT=3")), 3)

	def test_pathological_description_is_linear(self):
		started = time.monotonic()
		text = ical.plain_text("<a" * 50000 + " <b>bold</b><br>next")
		self.assertLess(time.monotonic() - started, 2)
		self.assertNotIn("<b>", text)

	def test_poisoned_object_is_skipped_until_it_changes(self):
		account = make_carddav_account(
			calendar_url="https://cloud.example.test/remote.php/dav/calendars/alice/work/",
			calendar_enabled=1,
			calendar_since="2026-01-01",
		).name
		href = "/remote.php/dav/calendars/alice/work/bomb.ics"
		bomb = calendar(vevent("bomb", attendees=[("x@example.test", "X")], extra=["RRULE:FREQ=SECONDLY"]))
		fetched = []

		class Server:
			def __init__(self, account):
				pass

			def sync(self, token):
				return [(href, '"b1"')], [], "t", True

			def multiget(self, hrefs):
				fetched.append(hrefs)
				return [(href, '"b1"', bomb)]

		with patch.object(sync, "CalDAVClient", Server), patch.object(frappe.db, "commit"):
			self.assertEqual(sync.pull(account)["skipped"], 1)
			sync.pull(account)
		row = frappe.db.get_value(
			"Calendar Object", {"account": account, "href": href}, ["skipped", "error"], as_dict=True
		)
		self.assertEqual(row.skipped, 1)
		self.assertIn("Unsupported", row.error)
		self.assertEqual(len(fetched), 1)
