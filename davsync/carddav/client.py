import posixpath
from time import sleep
from urllib.parse import unquote, urljoin, urlparse
from xml.etree.ElementTree import ParseError

import frappe
import requests
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring
from frappe.utils.password import get_decrypted_password

TIMEOUT = 30
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
RETRY_BACKOFF = 2
MAX_RETRY_SLEEP = 30
MAX_ATTEMPTS = 3
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRY_METHODS = frozenset({"PROPFIND", "REPORT", "DELETE"})
PUT_RETRY_STATUSES = frozenset({429, 503})
GENERATED_BOOK_MARKERS = ("z-server-generated", "z-app-generated")
INVALID_TOKEN_STATUSES = frozenset({403, 409})
PREFERRED_BOOK_NAMES = ("kontakte", "contacts")
MULTIGET_CHUNK = 50
XML_HEADERS = {"Content-Type": "application/xml; charset=utf-8"}
NS = {"d": "DAV:", "card": "urn:ietf:params:xml:ns:carddav", "cal": "urn:ietf:params:xml:ns:caldav"}
NS_DECLARATIONS = (
	'xmlns:d="DAV:" xmlns:card="urn:ietf:params:xml:ns:carddav" xmlns:cal="urn:ietf:params:xml:ns:caldav"'
)


def retry_delay(response, attempt):
	retry_after = response.headers.get("Retry-After") if response is not None else None
	try:
		delay = float(retry_after)
	except TypeError, ValueError:
		delay = RETRY_BACKOFF**attempt
	return min(max(delay, 0), MAX_RETRY_SLEEP)


class CardDAVError(Exception):
	pass


class PreconditionFailed(CardDAVError):
	pass


class UnsafeURL(CardDAVError):
	pass


def origin(url):
	parsed = urlparse(url)
	return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port


def same_origin(url, base):
	return origin(url) == origin(base)


def clean_path(url):
	"""The decoded path, or None when it has dot segments or empty parts after decoding."""
	path = unquote(urlparse(url).path)
	normal = posixpath.normpath(path)
	if path.endswith("/"):
		normal += "/"
	return normal if normal == path and "//" not in path else None


def member_url(collection, href):
	"""The absolute URL of href when it names an object inside collection, else UnsafeURL."""
	url = urljoin(collection, href)
	base, path = clean_path(collection), clean_path(url)
	if not (base and path) or not same_origin(url, collection):
		raise UnsafeURL(f"{href} is outside the collection")
	base = base if base.endswith("/") else f"{base}/"
	if not path.startswith(base) or path.rstrip("/") == base.rstrip("/") or urlparse(url).query:
		raise UnsafeURL(f"{href} is outside the collection")
	return url


def qname(prefixed):
	prefix, name = prefixed.split(":")
	return f"{{{NS[prefix]}}}{name}"


def xml_body(inner):
	return f'<?xml version="1.0" encoding="utf-8"?>{inner}'.encode()


def escape(text):
	return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def parse_multistatus(response):
	try:
		root = fromstring(response.content)
	except (ParseError, DefusedXmlException):
		raise CardDAVError("Server returned invalid XML") from None
	entries = [parse_response(node) for node in root.findall("d:response", NS)]
	return entries, root.findtext("d:sync-token", "", NS).strip()


def parse_response(node):
	props = {}
	for propstat in node.findall("d:propstat", NS):
		if " 200 " in f" {propstat.findtext('d:status', '', NS)} ":
			for prop in propstat.findall("d:prop/*", NS):
				props[prop.tag] = prop
	return {
		"href": (node.findtext("d:href", "", NS) or "").strip(),
		"status": node.findtext("d:status", "", NS),
		"props": props,
	}


def prop_text(entry, name):
	node = entry["props"].get(qname(name))
	return (node.text or "").strip() if node is not None else ""


def prop_href(entry, name):
	node = entry["props"].get(qname(name))
	return (node.findtext("d:href", "", NS) or "").strip() if node is not None else ""


def is_collection(entry):
	resourcetype = entry["props"].get(qname("d:resourcetype"))
	return resourcetype is not None and len(resourcetype) > 0


def is_address_book(entry):
	resourcetype = entry["props"].get(qname("d:resourcetype"))
	return resourcetype is not None and resourcetype.find("card:addressbook", NS) is not None


class CardDAVClient:
	HOME_SET = "card:addressbook-home-set"
	MULTIGET = "card:addressbook-multiget"
	DATA = "card:address-data"
	MISSING = "Address book not configured"

	def __init__(self, account: str):
		values = frappe.get_doc("CardDAV Account", account)
		self.server_url = values.server_url
		self.address_book_url = values.address_book_url or ""
		self.session = requests.Session()
		self.session.auth = (
			values.username,
			get_decrypted_password("CardDAV Account", account, "app_password"),
		)

	def collections(self):
		principal = self.propfind(self.server_url, "<d:current-user-principal/>")
		principal_href = next((prop_href(e, "d:current-user-principal") for e in principal), "")
		if not principal_href:
			raise CardDAVError("Server did not report a user principal")
		principal_url = self.on_server(urljoin(self.server_url, principal_href))
		home = self.propfind(principal_url, f"<{self.HOME_SET}/>")
		home_href = next((prop_href(e, self.HOME_SET) for e in home), "")
		if not home_href:
			raise CardDAVError("Server did not report a collection home")
		home_url = self.on_server(urljoin(principal_url, home_href))
		return home_url, self.propfind(home_url, "<d:displayname/><d:resourcetype/>", depth=1)

	def on_server(self, url):
		if not same_origin(url, self.server_url):
			raise UnsafeURL("Server pointed to another host")
		return url

	def discover_address_book(self) -> str:
		home_url, entries = self.collections()
		books = [e for e in entries if is_address_book(e)]
		if not books:
			raise CardDAVError("No address book found")
		books = [b for b in books if not any(m in b["href"] for m in GENERATED_BOOK_MARKERS)]
		if not books:
			raise CardDAVError("No address book found")
		named = [b for b in books if prop_text(b, "d:displayname").lower() in PREFERRED_BOOK_NAMES]
		contacts = [b for b in books if b["href"].rstrip("/").endswith("/contacts")]
		return self.on_server(urljoin(home_url, (named or contacts or books)[0]["href"]))

	def sync(self, token: str | None) -> tuple[list[tuple[str, str]], list[str], str, bool]:
		self.require_book()
		if not token:
			return self.list_all()
		body = xml_body(
			f"<d:sync-collection {NS_DECLARATIONS}><d:sync-token>{escape(token)}</d:sync-token>"
			"<d:sync-level>1</d:sync-level><d:prop><d:getetag/></d:prop></d:sync-collection>"
		)
		response = self.request(
			"REPORT", self.collection_url(), headers=XML_HEADERS, data=body, allow=INVALID_TOKEN_STATUSES
		)
		if response.status_code in INVALID_TOKEN_STATUSES:
			return self.list_all()
		entries, new_token = parse_multistatus(response)
		changed, deleted = [], []
		for entry in entries:
			if entry["status"].find(" 404") >= 0:
				deleted.append(entry["href"])
			elif prop_text(entry, "d:getetag"):
				changed.append((entry["href"], prop_text(entry, "d:getetag")))
		return changed, deleted, new_token, False

	def list_all(self):
		entries = self.propfind(
			self.collection_url(), "<d:getetag/><d:resourcetype/><d:sync-token/>", depth=1
		)
		changed = [
			(e["href"], prop_text(e, "d:getetag"))
			for e in entries
			if prop_text(e, "d:getetag") and not is_collection(e)
		]
		token = next((prop_text(e, "d:sync-token") for e in entries if is_collection(e)), "")
		return changed, [], token, True

	def multiget(self, hrefs: list[str]) -> list[tuple[str, str, str]]:
		self.require_book()
		hrefs = [h for h in hrefs if self.is_member(h)]
		results = []
		for start in range(0, len(hrefs), MULTIGET_CHUNK):
			chunk = hrefs[start : start + MULTIGET_CHUNK]
			body = xml_body(
				f"<{self.MULTIGET} {NS_DECLARATIONS}><d:prop><d:getetag/><{self.DATA}/></d:prop>"
				+ "".join(f"<d:href>{escape(h)}</d:href>" for h in chunk)
				+ f"</{self.MULTIGET}>"
			)
			response = self.request("REPORT", self.collection_url(), headers=XML_HEADERS, data=body)
			for entry in parse_multistatus(response)[0]:
				text = entry["props"].get(qname(self.DATA))
				if text is not None and text.text and self.is_member(entry["href"]):
					results.append((entry["href"], prop_text(entry, "d:getetag"), text.text))
		return results

	def put(self, href: str, text: str, etag: str | None) -> str:
		self.require_book()
		url = self.member(href)
		headers = {"Content-Type": "text/vcard; charset=utf-8"}
		headers.update({"If-Match": etag} if etag else {"If-None-Match": "*"})
		response = self.request("PUT", url, headers=headers, data=text.encode(), allow={412})
		if response.status_code == 412:
			raise PreconditionFailed(f"PUT {urlparse(url).path} precondition failed")
		new_etag = response.headers.get("ETag")
		if new_etag:
			return new_etag
		entries = self.propfind(url, "<d:getetag/>")
		return next((prop_text(e, "d:getetag") for e in entries), "")

	def delete(self, href: str, etag: str) -> None:
		self.require_book()
		if not etag:
			raise CardDAVError("Refusing to delete without an ETag")
		url = self.member(href)
		response = self.request("DELETE", url, headers={"If-Match": etag}, allow={404, 412})
		if response.status_code == 412:
			raise PreconditionFailed(f"DELETE {urlparse(url).path} precondition failed")

	def member(self, href):
		return member_url(self.collection_url(), href)

	def is_member(self, href):
		try:
			self.member(href)
		except UnsafeURL:
			frappe.log_error(title="DAV href outside the collection", message=href[:500])
			return False
		return True

	def propfind(self, url, props, depth=0):
		body = xml_body(f"<d:propfind {NS_DECLARATIONS}><d:prop>{props}</d:prop></d:propfind>")
		headers = {**XML_HEADERS, "Depth": str(depth)}
		return parse_multistatus(self.request("PROPFIND", url, headers=headers, data=body))[0]

	def collection_url(self):
		return self.address_book_url

	def require_book(self):
		if not self.collection_url():
			raise CardDAVError(self.MISSING)

	def absolute(self, href):
		return urljoin(self.collection_url() or self.server_url, href)

	def request(self, method, href, allow=(), **kwargs):
		url = self.on_server(self.absolute(href))
		path = urlparse(url).path
		for attempt in range(1, MAX_ATTEMPTS + 1):
			response, error = self.send(method, url, path, **kwargs)
			status = response.status_code if response is not None else None
			if not self.should_retry(method, status) or attempt == MAX_ATTEMPTS:
				break
			sleep(retry_delay(response, attempt))
		if error:
			raise error from None
		if 300 <= status < 400:
			raise CardDAVError(f"{method} {path} was redirected; set the final URL instead")
		if status >= 400 and status not in allow:
			raise CardDAVError(f"{method} {path} failed with HTTP {status}")
		return response

	def send(self, method, url, path, **kwargs):
		try:
			response = self.session.request(
				method, url, timeout=TIMEOUT, allow_redirects=False, stream=True, **kwargs
			)
			read_capped(response)
			return response, None
		except requests.RequestException as exc:
			return None, CardDAVError(f"{method} {path} failed: {type(exc).__name__}")

	@staticmethod
	def should_retry(method, status):
		if method == "PUT":
			return status in PUT_RETRY_STATUSES
		if method in RETRY_METHODS:
			return status is None or status in RETRY_STATUSES
		return status == 429


def read_capped(response):
	chunks, size = [], 0
	for chunk in response.iter_content(CHUNK_BYTES):
		size += len(chunk)
		if size > MAX_RESPONSE_BYTES:
			response.close()
			raise CardDAVError("Server response is too large")
		chunks.append(chunk)
	response._content = b"".join(chunks)
	response._content_consumed = True
