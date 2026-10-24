from unittest.mock import patch

import requests
from frappe.tests import IntegrationTestCase

from davsync.carddav import client as client_module
from davsync.carddav.client import CardDAVClient, CardDAVError, PreconditionFailed
from davsync.tests.helpers import guard_commit, make_carddav_account

PASSWORD = "app-pw-9f8e7d6c"
BOOK = "https://cloud.example.test/remote.php/dav/addressbooks/users/alice/contacts/"
BOOK_PATH = "/remote.php/dav/addressbooks/users/alice/contacts/"
NS = 'xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav" xmlns:cs="http://calendarserver.org/ns/"'


class FakeResponse:
	def __init__(self, body="", status_code=207, headers=None):
		self.content = body.encode()
		self.status_code = status_code
		self.headers = headers or {}

	def iter_content(self, size):
		yield self.content

	def close(self):
		pass


def multistatus(*responses, token=None):
	tail = f"<d:sync-token>{token}</d:sync-token>" if token else ""
	return FakeResponse(
		f'<?xml version="1.0"?><d:multistatus {NS}>{"".join(responses)}{tail}</d:multistatus>'
	)


def entry(href, props="", status="200 OK"):
	return (
		f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop>"
		f"<d:status>HTTP/1.1 {status}</d:status></d:propstat></d:response>"
	)


def gone(href):
	return f"<d:response><d:href>{href}</d:href><d:status>HTTP/1.1 404 Not Found</d:status></d:response>"


def book(name, display):
	return entry(
		f"/remote.php/dav/addressbooks/users/alice/{name}/",
		f"<d:displayname>{display}</d:displayname><d:resourcetype><d:collection/><card:addressbook/></d:resourcetype>",
	)


class TestCardDAVClient(IntegrationTestCase):
	def setUp(self):
		account = make_carddav_account(app_password=PASSWORD, address_book_url=BOOK)
		self.client = CardDAVClient(account.name)
		guard_commit(self)
		patcher = patch.object(client_module, "sleep")
		self.sleep = patcher.start()
		self.addCleanup(patcher.stop)

	def patched(self, responses):
		queue = list(responses)
		return patch.object(
			requests.Session, "request", autospec=True, side_effect=lambda *a, **k: queue.pop(0)
		)

	def test_discover_prefers_contacts_book(self):
		principal = entry(
			"/remote.php/dav/",
			"<d:current-user-principal><d:href>/remote.php/dav/principals/users/alice/</d:href></d:current-user-principal>",
		)
		home = entry(
			"/remote.php/dav/principals/users/alice/",
			"<card:addressbook-home-set><d:href>/remote.php/dav/addressbooks/users/alice/</d:href></card:addressbook-home-set>",
		)
		listing = multistatus(
			entry(
				"/remote.php/dav/addressbooks/users/alice/",
				"<d:resourcetype><d:collection/></d:resourcetype>",
			),
			book("zzz", "Other"),
			book("contacts", "Kontakte"),
		)
		with self.patched([multistatus(principal), multistatus(home), listing]) as request:
			url = self.client.discover_address_book()
		self.assertEqual(url, "https://cloud.example.test/remote.php/dav/addressbooks/users/alice/contacts/")
		self.assertEqual(
			request.call_args_list[2].args[2],
			"https://cloud.example.test/remote.php/dav/addressbooks/users/alice/",
		)
		self.assertEqual(request.call_args_list[0].args[1], "PROPFIND")

	def test_discover_falls_back_to_first_book(self):
		principal = entry("/", "<d:current-user-principal><d:href>/p/</d:href></d:current-user-principal>")
		home = entry("/p/", "<card:addressbook-home-set><d:href>/ab/</d:href></card:addressbook-home-set>")
		listing = multistatus(
			entry(
				"/ab/x/",
				"<d:displayname>X</d:displayname><d:resourcetype><card:addressbook/></d:resourcetype>",
			)
		)
		with self.patched([multistatus(principal), multistatus(home), listing]):
			self.assertEqual(self.client.discover_address_book(), "https://cloud.example.test/ab/x/")

	def test_sync_parses_changes_and_deletions(self):
		href = "/remote.php/dav/addressbooks/users/alice/contacts/a.vcf"
		response = multistatus(
			entry(href, '<d:getetag>"e1"</d:getetag>'),
			gone("/remote.php/dav/addressbooks/users/alice/contacts/b.vcf"),
			token="tok-2",
		)
		with self.patched([response]) as request:
			changed, deleted, token, full = self.client.sync("tok-1")
		self.assertFalse(full)
		self.assertEqual(changed, [(href, '"e1"')])
		self.assertEqual(deleted, ["/remote.php/dav/addressbooks/users/alice/contacts/b.vcf"])
		self.assertEqual(token, "tok-2")
		self.assertEqual(request.call_args.args[1], "REPORT")
		self.assertIn(b"tok-1", request.call_args.kwargs["data"])

	def test_invalid_token_falls_back_to_full_listing(self):
		for status in (403, 409):
			listing = multistatus(
				entry(
					BOOK_PATH,
					"<d:resourcetype><d:collection/><card:addressbook/></d:resourcetype><d:sync-token>fresh</d:sync-token>",
				),
				entry(f"{BOOK_PATH}a.vcf", '<d:getetag>"e1"</d:getetag>'),
			)
			with self.patched([FakeResponse("", status), listing]) as request:
				result = self.client.sync("stale")
			self.assertEqual(result, ([(f"{BOOK_PATH}a.vcf", '"e1"')], [], "fresh", True))
			self.assertEqual(request.call_args.args[1], "PROPFIND")
			self.assertEqual(request.call_args.kwargs["headers"]["Depth"], "1")

	def test_no_token_lists_everything(self):
		listing = multistatus(entry(f"{BOOK_PATH}a.vcf", '<d:getetag>"e1"</d:getetag>'))
		with self.patched([listing]) as request:
			changed, _, token, full = self.client.sync(None)
		self.assertTrue(full)
		self.assertEqual(changed, [(f"{BOOK_PATH}a.vcf", '"e1"')])
		self.assertEqual(token, "")
		self.assertEqual(request.call_args.args[1], "PROPFIND")

	def test_multiget_returns_vcards(self):
		data = "<card:address-data>BEGIN:VCARD\nFN:A\nEND:VCARD\n</card:address-data>"
		response = multistatus(
			entry(f"{BOOK_PATH}a.vcf", f'<d:getetag>"e1"</d:getetag>{data}'), gone(f"{BOOK_PATH}b.vcf")
		)
		with self.patched([response]):
			result = self.client.multiget([f"{BOOK_PATH}a.vcf", f"{BOOK_PATH}b.vcf"])
		self.assertEqual(result, [(f"{BOOK_PATH}a.vcf", '"e1"', "BEGIN:VCARD\nFN:A\nEND:VCARD\n")])

	def test_put_sends_if_match_and_returns_etag(self):
		with self.patched([FakeResponse("", 204, {"ETag": '"e2"'})]) as request:
			etag = self.client.put(f"{BOOK_PATH}a.vcf", "BEGIN:VCARD\nEND:VCARD\n", '"e1"')
		self.assertEqual(etag, '"e2"')
		headers = request.call_args.kwargs["headers"]
		self.assertEqual(headers["If-Match"], '"e1"')
		self.assertNotIn("If-None-Match", headers)
		self.assertEqual(request.call_args.args[2], BOOK + "a.vcf")

	def test_put_without_etag_creates_only(self):
		with self.patched([FakeResponse("", 201, {"ETag": '"e1"'})]) as request:
			self.client.put(f"{BOOK_PATH}a.vcf", "x", None)
		self.assertEqual(request.call_args.kwargs["headers"]["If-None-Match"], "*")

	def test_put_without_etag_header_reads_it_back(self):
		listing = multistatus(entry(f"{BOOK_PATH}a.vcf", '<d:getetag>"e9"</d:getetag>'))
		with self.patched([FakeResponse("", 201), listing]):
			self.assertEqual(self.client.put(f"{BOOK_PATH}a.vcf", "x", None), '"e9"')

	def test_put_412_raises_precondition_failed(self):
		with self.patched([FakeResponse("", 412)]):
			with self.assertRaises(PreconditionFailed):
				self.client.put(f"{BOOK_PATH}a.vcf", "x", '"old"')

	def test_delete_sends_if_match_and_tolerates_missing(self):
		with self.patched([FakeResponse("", 204), FakeResponse("", 404)]) as request:
			self.client.delete(f"{BOOK_PATH}a.vcf", '"e1"')
			self.client.delete(f"{BOOK_PATH}a.vcf", '"e2"')
		self.assertEqual(request.call_args_list[0].kwargs["headers"]["If-Match"], '"e1"')

	def test_retries_server_errors_then_succeeds(self):
		with self.patched([FakeResponse("", 503), FakeResponse("", 204, {"ETag": '"e"'})]) as request:
			self.client.put(f"{BOOK_PATH}a.vcf", "x", '"e1"')
		self.assertEqual(request.call_count, 2)
		self.sleep.assert_called_once()

	def test_put_not_retried_on_connection_error(self):
		boom = requests.ConnectionError("down")
		with patch.object(requests.Session, "request", autospec=True, side_effect=boom) as request:
			with self.assertRaises(CardDAVError):
				self.client.put(f"{BOOK_PATH}a.vcf", "x", '"e1"')
		self.assertEqual(request.call_count, 1)

	def test_put_not_retried_on_500_but_retried_on_503(self):
		with self.patched([FakeResponse("", 500)]) as request:
			with self.assertRaises(CardDAVError):
				self.client.put(f"{BOOK_PATH}a.vcf", "x", '"e1"')
		self.assertEqual(request.call_count, 1)
		with self.patched([FakeResponse("", 503), FakeResponse("", 204, {"ETag": '"e"'})]) as request:
			self.client.put(f"{BOOK_PATH}a.vcf", "x", '"e1"')
		self.assertEqual(request.call_count, 2)

	def test_delete_retried_on_connection_error(self):
		queue = [requests.ConnectionError("down"), FakeResponse("", 404)]

		def respond(*args, **kwargs):
			item = queue.pop(0)
			if isinstance(item, Exception):
				raise item
			return item

		with patch.object(requests.Session, "request", autospec=True, side_effect=respond) as request:
			self.client.delete(f"{BOOK_PATH}a.vcf", '"e1"')
		self.assertEqual(request.call_count, 2)

	def test_unconfigured_address_book_raises(self):
		self.client.address_book_url = ""
		for call in (
			lambda: self.client.sync("t"),
			lambda: self.client.multiget(["/a"]),
			lambda: self.client.put("/a", "x", None),
			lambda: self.client.delete("/a", None),
		):
			with self.assertRaisesRegex(CardDAVError, "not configured"):
				call()

	def test_discover_skips_generated_books_and_prefers_contacts_path(self):
		principal = entry("/", "<d:current-user-principal><d:href>/p/</d:href></d:current-user-principal>")
		home = entry("/p/", "<card:addressbook-home-set><d:href>/ab/</d:href></card:addressbook-home-set>")
		kind = "<d:resourcetype><card:addressbook/></d:resourcetype>"
		listing = multistatus(
			entry("/ab/z-server-generated--system/", f"<d:displayname>Contacts</d:displayname>{kind}"),
			entry("/ab/other/", f"<d:displayname>A</d:displayname>{kind}"),
			entry("/ab/contacts/", f"<d:displayname>B</d:displayname>{kind}"),
		)
		with self.patched([multistatus(principal), multistatus(home), listing]):
			self.assertEqual(self.client.discover_address_book(), "https://cloud.example.test/ab/contacts/")

	def test_auth_and_timeout(self):
		with self.patched([FakeResponse("", 204, {"ETag": '"e"'})]) as request:
			self.client.put(f"{BOOK_PATH}a.vcf", "x", '"e1"')
		self.assertEqual(self.client.session.auth, ("alice", PASSWORD))
		self.assertEqual(request.call_args.kwargs["timeout"], 30)

	def test_password_never_in_error(self):
		boom = requests.ConnectionError(f"https://alice:{PASSWORD}@cloud.example.test failed")
		with patch.object(requests.Session, "request", autospec=True, side_effect=boom):
			with self.assertRaises(CardDAVError) as ctx:
				self.client.delete(f"{BOOK_PATH}a.vcf", '"e"')
		self.assertNotIn(PASSWORD, str(ctx.exception))
		self.assertIsNone(ctx.exception.__cause__)
		with self.patched([FakeResponse(f"echo {PASSWORD}", 500)] * 3):
			with self.assertRaises(CardDAVError) as ctx:
				self.client.delete(f"{BOOK_PATH}a.vcf", '"e"')
		self.assertNotIn(PASSWORD, str(ctx.exception))
