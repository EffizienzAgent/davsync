from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from davsync.carddav import api, pull, push, vcard
from davsync.carddav.adapter import ContactAdapter
from davsync.carddav.client import CardDAVError, PreconditionFailed
from davsync.tests.helpers import (
	guard_commit,
	make_carddav_account,
	make_contact,
	make_user,
	only_accounts,
	settle_contacts,
	tag,
	use_hooks,
)

BOOK = "https://cloud.example.test/remote.php/dav/addressbooks/users/alice/contacts/"
BOOK_PATH = "/remote.php/dav/addressbooks/users/alice/contacts/"


def vcf(uid, given, family, *extra):
	lines = ["BEGIN:VCARD", "VERSION:3.0", f"UID:{uid}", f"N:{family};{given};;;", f"FN:{given} {family}"]
	lines += list(extra)
	lines.append("END:VCARD")
	return "\r\n".join(lines) + "\r\n"


class FakeServer:
	cards: dict = {}  # noqa: RUF012
	calls: list = []  # noqa: RUF012
	deleted: list = []  # noqa: RUF012
	full = False

	def __init__(self, account):
		self.account = account

	@staticmethod
	def new_etag():
		return f'"{tag()}"'

	def put(self, href, text, etag):
		FakeServer.calls.append(("put", href, etag))
		current = FakeServer.cards.get(href)
		if (etag is None and current) or (etag is not None and (not current or current[0] != etag)):
			raise PreconditionFailed("PUT precondition failed")
		FakeServer.cards[href] = (self.new_etag(), text)
		return FakeServer.cards[href][0]

	def delete(self, href, etag):
		FakeServer.calls.append(("delete", href, etag))
		FakeServer.cards.pop(href, None)

	def multiget(self, hrefs):
		return [(h, *FakeServer.cards[h]) for h in hrefs if h in FakeServer.cards]

	def sync(self, token):
		changed = [(href, etag) for href, (etag, _text) in FakeServer.cards.items()]
		return changed, list(FakeServer.deleted), "token-next", FakeServer.full


class SharedByCompany(ContactAdapter):
	def is_shared(self, contact):
		return bool(frappe.db.get_value("Contact", contact, "company_name"))

	def visible_condition(self, user):
		return f"(`tabContact`.`owner` = {frappe.db.escape(user)} or ifnull(`tabContact`.`company_name`, '') != '')"

	def organization_name(self, doc):
		return f"{doc.company_name} (shared)" if doc.company_name else ""

	def keep_email(self, row):
		return row.email_id.startswith("keep-")


class ContactSyncCase(IntegrationTestCase):
	def setUp(self):
		self.owner = make_user("davsync-owner@example.test", roles=("System Manager",))
		self.account = make_carddav_account(
			address_book_url=BOOK, owner_user=self.owner, enabled=1, push_enabled=1
		).name
		guard_commit(self)
		only_accounts(self, self.account)
		settle_contacts()
		FakeServer.cards, FakeServer.calls, FakeServer.deleted, FakeServer.full = {}, [], [], False
		for module in (push, pull):
			patcher = patch.object(module, "CardDAVClient", FakeServer)
			patcher.start()
			self.addCleanup(patcher.stop)
		self.addCleanup(frappe.db.after_commit.reset)

	def card(self, given, family, *extra, uid=None):
		href = f"{BOOK_PATH}{tag()}.vcf"
		FakeServer.cards[href] = (FakeServer.new_etag(), vcf(uid or f"uid-{tag()}", given, family, *extra))
		return href

	def pull(self, initial=False):
		with patch.object(frappe.db, "commit"):
			return pull.pull(self.account, initial)

	def contact_at(self, href):
		return frappe.get_doc("Contact", {"carddav_account": self.account, "carddav_href": href})

	def remote(self, href):
		return vcard.parse(FakeServer.cards[href][1])


class TestPull(ContactSyncCase):
	def test_new_card_creates_contact_owned_by_account_owner(self):
		email = f"erika-{tag()}@example.test"
		href = self.card(
			"Erika", "Muster", f"EMAIL;TYPE=WORK:{email}", "ORG:Beispiel AG", "TEL;TYPE=CELL:+49 170 1234567"
		)
		counts = self.pull()
		doc = self.contact_at(href)
		self.assertEqual(counts["created"], 1)
		self.assertEqual(
			(doc.first_name, doc.last_name, doc.company_name), ("Erika", "Muster", "Beispiel AG")
		)
		self.assertEqual([e.email_id for e in doc.email_ids], [email])
		self.assertEqual([p.phone for p in doc.phone_nos], ["+491701234567"])
		self.assertEqual(doc.owner, self.owner)
		self.assertEqual(frappe.db.get_value("CardDAV Account", self.account, "sync_token"), "token-next")

	def test_unchanged_etag_is_skipped_and_second_pull_does_not_duplicate(self):
		href = self.card("Max", "Beispiel")
		self.pull()
		counts = self.pull()
		self.assertEqual((counts["created"], counts["skipped"]), (0, 1))
		self.assertEqual(
			frappe.db.count("Contact", {"carddav_account": self.account, "carddav_href": href}), 1
		)

	def test_existing_contact_is_adopted_by_email(self):
		email = f"adopt-{tag()}@example.test"
		existing = make_contact("Ada", "Adopt", emails=[email], owner=self.owner)
		frappe.db.set_value("Contact", existing.name, "owner", self.owner)
		href = self.card("Ada", "Adopt", f"EMAIL:{email}")
		counts = self.pull()
		self.assertEqual(counts["adopted"], 1)
		self.assertEqual(self.contact_at(href).name, existing.name)

	def test_deleted_card_archives_contact(self):
		href = self.card("Gone", "Soon")
		self.pull()
		FakeServer.cards.pop(href)
		FakeServer.deleted = [href]
		counts = self.pull()
		self.assertEqual(counts["archived"], 1)
		self.assertEqual(frappe.db.get_value("Contact", self.contact_at(href).name, "archived"), 1)

	def test_full_listing_does_not_mass_archive(self):
		hrefs = [self.card("Many", f"Contact {i}") for i in range(pull.MASS_ARCHIVE_COUNT + 1)]
		self.pull()
		FakeServer.cards = {}
		FakeServer.full = True
		counts = self.pull()
		self.assertEqual(counts["archived"], 0)
		self.assertFalse(
			any(frappe.db.get_value("Contact", self.contact_at(h).name, "archived") for h in hrefs)
		)
		self.assertIn("skipped", frappe.db.get_value("CardDAV Account", self.account, "last_error"))

	def test_group_cards_are_skipped(self):
		href = f"{BOOK_PATH}{tag()}.vcf"
		text = "BEGIN:VCARD\r\nVERSION:3.0\r\nUID:g-1\r\nFN:Team\r\nX-ADDRESSBOOKSERVER-KIND:group\r\nEND:VCARD\r\n"
		FakeServer.cards[href] = ('"g"', text)
		self.assertEqual(self.pull()["skipped"], 1)

	def test_phone_region_comes_from_settings(self):
		frappe.get_single("DAV Sync Settings").update({"phone_region": "AT"}).save()
		self.addCleanup(lambda: frappe.get_single("DAV Sync Settings").update({"phone_region": ""}).save())
		href = self.card("Region", "Test", "TEL;TYPE=WORK:0664 1234567")
		self.pull()
		self.assertEqual([p.phone for p in self.contact_at(href).phone_nos], ["+436641234567"])

	def test_failed_pull_keeps_token_and_records_error(self):
		with patch.object(pull, "pull", side_effect=CardDAVError("401 for app-pw-1234")):
			self.assertFalse(pull.run_pull(self.account))
		error = frappe.db.get_value("CardDAV Account", self.account, "last_error")
		self.assertIn("***", error)
		self.assertNotIn("app-pw-1234", error)


class TestPush(ContactSyncCase):
	def push(self, contact):
		with patch.object(frappe.db, "commit"):
			return push.push_contact(self.account, contact)

	def test_own_contact_is_created_on_server(self):
		doc = make_contact("Neu", "Kontakt", emails=[f"neu-{tag()}@example.test"])
		frappe.db.set_value("Contact", doc.name, "owner", self.owner)
		self.assertEqual(self.push(doc.name), "created")
		doc.reload()
		self.assertTrue(doc.carddav_href.startswith(BOOK_PATH))
		remote = self.remote(doc.carddav_href)
		self.assertEqual((remote.given, remote.family), ("Neu", "Kontakt"))
		self.assertEqual(self.push(doc.name), "unchanged")

	def test_contact_of_other_user_is_not_pushed(self):
		doc = make_contact("Fremd", "Kontakt")
		self.assertEqual(self.push(doc.name), "skipped")
		self.assertEqual(FakeServer.calls, [])

	def test_pulled_contact_is_not_pushed_back(self):
		href = self.card("Ruhig", "Bleiben")
		with patch.object(frappe, "enqueue") as enqueue:
			self.pull()
		self.assertFalse([c for c in enqueue.call_args_list if c.args and "carddav" in c.args[0]])
		self.assertEqual(self.push(self.contact_at(href).name), "unchanged")
		self.assertEqual([c for c in FakeServer.calls if c[0] == "put"], [])

	def test_conflict_merges_remote_card(self):
		href = self.card("Konflikt", "Fall")
		self.pull()
		doc = self.contact_at(href)
		_etag, text = FakeServer.cards[href]
		FakeServer.cards[href] = ('"remote"', text.replace("END:VCARD", "NOTE:remote note\r\nEND:VCARD"))
		doc.first_name = "Lokal"
		doc.save()
		self.assertEqual(self.push(doc.name), "updated")
		remote = self.remote(href)
		self.assertEqual((remote.given, remote.note), ("Lokal", "remote note"))

	def test_archived_contact_is_deleted_on_server(self):
		href = self.card("Weg", "Damit")
		self.pull()
		doc = self.contact_at(href)
		doc.archived = 1
		doc.save()
		self.assertEqual(self.push(doc.name), "deleted")
		self.assertNotIn(href, FakeServer.cards)

	def test_trash_enqueues_delete(self):
		href = self.card("Trash", "Me")
		self.pull()
		doc = self.contact_at(href)
		with patch.object(frappe, "enqueue") as enqueue:
			frappe.delete_doc("Contact", doc.name)
		calls = [
			c for c in enqueue.call_args_list if c.args and c.args[0] == "davsync.carddav.push.run_delete"
		]
		self.assertEqual(calls[0].kwargs["href"], href)

	def test_adapter_shares_contacts_and_names_organization(self):
		use_hooks(self, davsync_contact_adapter=["davsync.tests.test_contacts.SharedByCompany"])
		doc = make_contact("Geteilt", "Kontakt", company_name="Beispiel AG")
		self.assertEqual(self.push(doc.name), "created")
		self.assertEqual(
			self.remote(frappe.db.get_value("Contact", doc.name, "carddav_href")).org, "Beispiel AG (shared)"
		)
		other = make_contact("Auch", "Geteilt", company_name="Beispiel AG")
		hidden = make_contact("Nicht", "Geteilt")
		old = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-1)
		for name in (other.name, hidden.name):
			frappe.db.set_value("Contact", name, "modified", old, update_modified=False)
		frappe.db.set_value("CardDAV Account", self.account, {"sync_token": "t", "last_error": ""})
		pending = push.pending_contacts(self.account, self.owner)
		self.assertIn(other.name, pending)
		self.assertNotIn(hidden.name, pending)

	def test_adapter_keeps_emails(self):
		use_hooks(self, davsync_contact_adapter=["davsync.tests.test_contacts.SharedByCompany"])
		email = f"keep-{tag()}@example.test"
		href = self.card("Behalt", "Mail")
		self.pull()
		doc = self.contact_at(href)
		doc.append("email_ids", {"email_id": email})
		doc.save()
		_etag, text = FakeServer.cards[href]
		FakeServer.cards[href] = ('"changed"', text.replace("FN:", "TITLE:Chef\r\nFN:"))
		self.pull()
		self.assertIn(email, [e.email_id for e in self.contact_at(href).email_ids])

	def test_push_all_reports_counts(self):
		frappe.db.set_value("Contact", make_contact("Alle", "Senden").name, "owner", self.owner)
		with patch.object(frappe.db, "commit"):
			push.run_push_all(self.account)
		self.assertIn("on the server", frappe.db.get_value("CardDAV Account", self.account, "last_result"))


class TestApi(ContactSyncCase):
	def test_start_sync_enqueues_deduplicated_job(self):
		with patch.object(frappe, "enqueue") as enqueue:
			api.start_sync(self.account)
		self.assertEqual(enqueue.call_args.args[0], "davsync.carddav.pull.run_pull")
		self.assertEqual(enqueue.call_args.kwargs["job_id"], f"carddav-sync-{self.account}")

	def test_unknown_account_is_rejected(self):
		with self.assertRaises(frappe.ValidationError):
			api.start_sync("missing-account")
