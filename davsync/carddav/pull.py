import traceback
from contextlib import contextmanager

import frappe
import phonenumbers
from frappe.core.doctype.file.utils import get_content_hash
from frappe.utils import add_to_date, cstr, now_datetime, validate_email_address
from frappe.utils.background_jobs import is_job_enqueued
from frappe.utils.html_utils import sanitize_html
from frappe.utils.password import get_decrypted_password

from davsync.carddav import vcard
from davsync.carddav.adapter import get_adapter
from davsync.carddav.client import CardDAVClient
from davsync.carddav.names import normalize_person, person_key
from davsync.phone import normalize_phone
from davsync.settings import phone_region

SAVEPOINT = "carddav_pull"
RUN_SAVEPOINT = "carddav_run"
COUNTS = ("created", "adopted", "updated", "pushed", "archived", "organizations", "skipped", "failed")
LONG_JOBS = ("sync", "initial-import", "push-all")
MASS_ARCHIVE_COUNT = 20
MASS_ARCHIVE_SHARE = 0.3
PENDING_SLACK_SECONDS = 2
ROLE_LOCALPARTS = (
	"rechnung",
	"buchhaltung",
	"invoice",
	"invoices",
	"billing",
	"accounting",
	"info",
	"office",
	"kontakt",
	"contact",
	"mail",
)
UPDATED_ADDRESS_FIELDS = ("address_line2", "state", "country", "carddav_type")
ADDRESS_TYPES = {"home": "Personal", "work": "Office"}
NO_NAME = "-"
PHOTO_PREFIX = "carddav-photo-"
COUNTRY_ALIASES = {
	"deutschland": "Germany",
	"österreich": "Austria",
	"schweiz": "Switzerland",
	"niederlande": "Netherlands",
	"belgien": "Belgium",
	"dänemark": "Denmark",
	"frankreich": "France",
	"italien": "Italy",
	"spanien": "Spain",
	"polen": "Poland",
	"schweden": "Sweden",
	"vereinigtes königreich": "United Kingdom",
	"großbritannien": "United Kingdom",
	"usa": "United States",
	"vereinigte staaten": "United States",
}


@contextmanager
def pull_flags(initial):
	flags = frappe.flags
	previous = flags.in_carddav_pull, flags.in_carddav_import, flags.carddav_cache
	flags.in_carddav_pull = True
	flags.in_carddav_import = bool(initial or previous[1])
	flags.carddav_cache = previous[2] if previous[2] is not None else {}
	try:
		yield
	finally:
		flags.in_carddav_pull, flags.in_carddav_import, flags.carddav_cache = previous


def run_cache():
	return frappe.flags.carddav_cache if frappe.flags.carddav_cache is not None else {}


def pull(account: str, initial: bool = False) -> dict:
	client = CardDAVClient(account)
	old_token = frappe.db.get_value("CardDAV Account", account, "sync_token")
	changed, deleted, new_token, full = client.sync(None if initial else old_token or None)
	known = {
		row.carddav_href: row
		for row in frappe.get_all(
			"Contact",
			filters={"carddav_account": account, "carddav_href": ("is", "set")},
			fields=["carddav_href", "carddav_etag", "archived"],
		)
	}
	counts = dict.fromkeys(COUNTS, 0)
	etags = dict(changed)
	gone = set(deleted)

	def present(href):
		return href in etags or not (full or href in gone)

	todo = [href for href, etag in changed if not etag or known.get(href, {}).get("carddav_etag") != etag]
	counts["skipped"] += len(changed) - len(todo)
	notes = []
	with pull_flags(initial):
		for href, etag, text in client.multiget(todo) if todo else []:
			counts[pull_one(account, href, etag or etags.get(href), text, initial, present)] += 1
		if full and not initial:
			missing = {href for href, row in known.items() if href not in etags and not row.archived}
			if is_mass_archive(len(missing), sum(1 for row in known.values() if not row.archived)):
				notes.append(
					frappe._("Full listing would archive {0} contacts — skipped.").format(len(missing))
				)
			else:
				gone |= missing
		for href in gone:
			counts["archived"] += archive_href(account, href)
	if counts["failed"]:
		notes.append(failure_note(counts["failed"]))
	values = {"last_error": " ".join(notes)}
	if not notes:
		values.update(sync_token=new_token, last_sync=now_datetime())
	frappe.db.set_value("CardDAV Account", account, values)
	return counts


def is_mass_archive(missing, linked):
	return missing > MASS_ARCHIVE_COUNT and missing > MASS_ARCHIVE_SHARE * linked


def enabled_accounts():
	return frappe.get_all("CardDAV Account", filters={"enabled": 1}, pluck="name", order_by="creation asc")


def job_running(account, actions=LONG_JOBS):
	return any(is_job_enqueued(f"carddav-{action}-{account}") for action in actions)


def pull_all() -> None:
	from davsync.carddav import push

	for account in enabled_accounts():
		if job_running(account):
			continue
		pulled = run_pull(account)
		frappe.db.commit()
		if pulled:
			push.push_pending(account)


def run_pull(account, initial=False):
	frappe.db.savepoint(RUN_SAVEPOINT)
	try:
		counts = pull(account, initial)
	except Exception as exc:
		frappe.db.rollback(save_point=RUN_SAVEPOINT)
		record_error(account, exc)
		return False
	summary = result_summary(counts)
	if initial:
		summary += " " + frappe._("Check the possible duplicates report.")
	frappe.db.set_value("CardDAV Account", account, "last_result", summary)
	return True


def record_error(account, exc):
	frappe.log_error(
		title="CardDAV sync failed",
		message=failure_trace(exc),
		reference_doctype="CardDAV Account",
		reference_name=account,
	)
	message = f"{type(exc).__name__}: {exc}"
	secret = get_decrypted_password("CardDAV Account", account, "app_password", raise_exception=False)
	if secret:
		message = message.replace(secret, "***")
	frappe.db.set_value("CardDAV Account", account, "last_error", message[:500])


def result_summary(counts):
	return frappe._(
		"Created {0}, adopted {1}, updated {2}, sent {3}, archived {4}, organizations {5}, skipped {6}, failed {7}."
	).format(*(counts[key] for key in COUNTS))


def failure_note(failed):
	return frappe._("{0} vCards could not be synced; see Error Log.").format(failed) if failed else ""


def pull_one(account, href, etag, text, initial, present=None):
	uid = href
	frappe.db.savepoint(SAVEPOINT)
	try:
		card = vcard.parse(text)
		uid = card.uid or href
		outcome = apply_card(account, href, etag, card, initial, present)[1]
	except Exception as exc:
		frappe.db.rollback(save_point=SAVEPOINT)
		frappe.clear_messages()
		frappe.log_error(
			title=f"CardDAV sync failed: {uid}",
			message=failure_trace(exc),
			reference_doctype="CardDAV Account",
			reference_name=account,
		)
		return "failed"
	frappe.db.release_savepoint(SAVEPOINT)
	return outcome


def failure_trace(exc):
	frames = traceback.extract_tb(exc.__traceback__)
	return "\n".join([type(exc).__name__, *(f"{f.filename}:{f.lineno} in {f.name}" for f in frames)])


def apply_card(account, href, etag, card, initial, present=None):
	from davsync.carddav import push

	if card.kind == "group":
		return None, "skipped"
	card.uid = card.uid or href
	adapter = get_adapter()
	name = frappe.db.get_value("Contact", {"carddav_account": account, "carddav_uid": card.uid})
	if not name and vcard.is_company(card):
		handled = adapter.company_card(card)
		if handled is not None:
			return handled
	if name and present and is_duplicate(name, href, present):
		frappe.log_error(
			title=f"CardDAV duplicate UID: {card.uid}",
			message=f"{href} skipped",
			reference_doctype="CardDAV Account",
			reference_name=account,
		)
		return name, "skipped"
	if name and frappe.db.get_value("Contact", name, "exclude_from_address_book"):
		if push.can_push(account):
			push.push_contact(account, name)
		return name, "skipped"
	if name and has_pending_push(name) and push.can_push(account):
		push.push_contact(account, name)
		return name, "pushed"
	if name:
		doc = frappe.get_doc("Contact", name)
		if doc.archived and not doc.carddav_etag:
			doc.archived = 0
		authoritative = True
		fill_contact(doc, card, adapter.contact_link(card), authoritative)
		outcome = "updated"
	else:
		if excluded_by_email(account, card):
			return None, "skipped"
		link = adapter.contact_link(card)
		name = adoptable_by_email(account, card) or adoptable_by_name(card, link)
		doc = frappe.get_doc("Contact", name) if name else frappe.new_doc("Contact")
		if name:
			adapter.on_adopt(doc)
		authoritative = False
		fill_contact(doc, card, link, authoritative)
		outcome = "adopted" if name else "created"
	doc.update(
		{
			"carddav_account": account,
			"carddav_uid": card.uid,
			"carddav_href": href,
			"carddav_etag": etag,
			"carddav_raw": card.raw,
		}
	)
	save_contact(doc, card, authoritative)
	if outcome == "created":
		doc.db_set("owner", frappe.db.get_value("CardDAV Account", account, "owner_user"))
	if outcome == "adopted":
		frappe.db.set_value("Contact", doc.name, "carddav_synced_at", None, update_modified=False)
	else:
		mark_synced(doc.name)
	return doc.name, outcome


def mark_synced(contact):
	frappe.db.set_value("Contact", contact, "carddav_synced_at", now_datetime(), update_modified=False)


def is_duplicate(name, href, present):
	row = frappe.db.get_value("Contact", name, ["carddav_href", "carddav_etag"], as_dict=True)
	return bool(row.carddav_href != href and row.carddav_etag and present(row.carddav_href))


def has_pending_push(contact):
	row = frappe.db.get_value("Contact", contact, ["modified", "carddav_synced_at"], as_dict=True)
	return is_pending(row.modified, row.carddav_synced_at)


def is_pending(modified, synced_at):
	return not synced_at or modified > add_to_date(synced_at, seconds=PENDING_SLACK_SECONDS)


def save_contact(doc, card, authoritative):
	if doc.is_new():
		doc.insert(ignore_permissions=True)
	else:
		doc.save(ignore_permissions=True)
	sync_addresses("Contact", doc.name, doc.full_name or doc.name, card.addresses, authoritative)
	set_photo(doc, card, authoritative)


def contacts_by_email(card):
	for address, _label in card.emails:
		names = frappe.get_all(
			"Contact Email",
			filters={"email_id": cstr(address).strip().lower(), "parenttype": "Contact"},
			pluck="parent",
			order_by="creation, idx",
		)
		yield from dict.fromkeys(names)


def is_reachable(row, name, owner):
	return row.owner == owner or get_adapter().is_shared(name)


def excluded_by_email(account, card):
	owner = frappe.db.get_value("CardDAV Account", account, "owner_user")
	for name in contacts_by_email(card):
		row = frappe.db.get_value("Contact", name, ["exclude_from_address_book", "owner"], as_dict=True)
		if row.exclude_from_address_book and is_reachable(row, name, owner):
			return True
	return False


def adoptable_by_email(account, card):
	owner = frappe.db.get_value("CardDAV Account", account, "owner_user")
	for name in contacts_by_email(card):
		if is_adoptable(name, owner):
			return name
	return None


def is_adoptable(name, owner):
	row = frappe.db.get_value(
		"Contact",
		name,
		["carddav_account", "archived", "exclude_from_address_book", "owner"],
		as_dict=True,
	)
	if row.archived or row.exclude_from_address_book or row.carddav_account:
		return False
	return is_reachable(row, name, owner)


def adoptable_by_name(card, link):
	key = person_key(card.given, card.family)
	link_doctype = get_adapter().link_doctype
	if not (link and link_doctype) or not key:
		return None
	linked = frappe.get_all(
		"Dynamic Link",
		filters={"parenttype": "Contact", "link_doctype": link_doctype, "link_name": link},
		pluck="parent",
	)
	contacts = frappe.get_all(
		"Contact",
		filters={
			"name": ("in", linked or [""]),
			"carddav_account": ("is", "not set"),
			"carddav_uid": ("is", "not set"),
			"archived": 0,
			"exclude_from_address_book": 0,
		},
		fields=["name", "first_name", "last_name"],
	)
	matches = [
		c.name for c in contacts if person_key(c.first_name, c.last_name) == key and not is_role_contact(c)
	]
	return matches[0] if len(matches) == 1 else None


def is_role_contact(contact):
	emails = frappe.get_all(
		"Contact Email", filters={"parent": contact.name, "parenttype": "Contact"}, pluck="email_id"
	)
	if not any(cstr(e).partition("@")[0].lower() in ROLE_LOCALPARTS for e in emails):
		return False
	names = {normalize_person(contact.first_name), normalize_person(contact.last_name)} - {""}
	return not names or bool(names & set(ROLE_LOCALPARTS))


def fill_contact(doc, card, link, authoritative):
	if card.given or card.family:
		doc.first_name, doc.last_name = card.given, card.family
	elif doc.is_new() or authoritative:
		doc.first_name, doc.last_name = fallback_name(card), ""
	for field, value in (
		("salutation", salutation(card.prefix)),
		("name_suffix", card.suffix),
		("designation", card.title),
		("birthday", card.birthday),
		("notes", card.note),
	):
		if value or authoritative:
			doc.set(field, value or None)
	link_doctype = get_adapter().link_doctype
	linked = bool(link_doctype) and any(row.link_doctype == link_doctype for row in doc.links)
	if not linked and link:
		doc.append("links", {"link_doctype": link_doctype, "link_name": link})
	elif not linked:
		doc.company_name = card.org or (None if authoritative else doc.company_name)
	set_emails(doc, card.emails, authoritative)
	set_phones(doc, card.phones, authoritative)


def fallback_name(card):
	if card.fn and card.fn != NO_NAME:
		return card.fn
	return card.org or next((e.partition("@")[0] for e, _label in card.emails if e), "") or NO_NAME


def salutation(prefix):
	prefix = cstr(prefix).strip()
	if not prefix:
		return None
	if not frappe.db.exists("Salutation", prefix):
		frappe.get_doc({"doctype": "Salutation", "salutation": prefix}).insert(ignore_permissions=True)
	return prefix


def email_of(row):
	return cstr(row.email_id).strip().lower()


def set_emails(doc, emails, authoritative):
	wanted = {}
	for address, label in emails:
		address = validate_email_address(cstr(address).strip().lower())
		if address:
			wanted.setdefault(address, label)
	if authoritative:
		keep = get_adapter().keep_email
		doc.set("email_ids", [row for row in doc.email_ids if keep(row) or email_of(row) in wanted])
	rows = {email_of(row): row for row in doc.email_ids}
	for address, label in wanted.items():
		row = rows.get(address) or doc.append("email_ids", {"email_id": address})
		row.carddav_type = label
	primary = next((r for r in doc.email_ids if r.is_primary), None) or next(
		(r for r in doc.email_ids if email_of(r) in wanted), None
	)
	for row in doc.email_ids:
		row.is_primary = int(row is (primary or doc.email_ids[0]))


def normalized_phone(raw):
	region = phone_region()
	try:
		number = phonenumbers.parse(raw, region)
	except phonenumbers.NumberParseException:
		return None
	if number.extension:
		return None
	try:
		return normalize_phone(raw, region)
	except frappe.ValidationError:
		frappe.clear_last_message()
		return None


def set_phones(doc, phones, authoritative):
	wanted, unparsed = [], []
	for raw, label in phones:
		raw = cstr(raw).strip()
		if not raw:
			continue
		phone = normalized_phone(raw)
		if phone is None:
			unparsed.append(raw)
		elif phone not in {p for p, _l in wanted}:
			wanted.append((phone, label))
	if authoritative:
		numbers = {p for p, _l in wanted}
		doc.set("phone_nos", [row for row in doc.phone_nos if phone_key(row) in numbers])
		doc.phone_unparsed = "\n".join(unparsed) or None
	else:
		lines = cstr(doc.phone_unparsed).splitlines()
		doc.phone_unparsed = "\n".join(lines + [u for u in unparsed if u not in lines]) or None
	existing = {phone_key(row): row for row in doc.phone_nos}
	for phone, label in wanted:
		row = existing.get(phone) or doc.append("phone_nos", {"phone": phone})
		row.carddav_type = label
	for flag, mobile in (("is_primary_mobile_no", True), ("is_primary_phone", False)):
		if any(row.get(flag) for row in doc.phone_nos):
			continue
		row = next((r for r in doc.phone_nos if is_mobile(r.carddav_type) == mobile), None)
		if row:
			row.set(flag, 1)


def phone_key(row):
	return normalized_phone(row.phone) or row.phone


def is_mobile(label):
	return bool({"cell", "iphone"} & set(cstr(label).split(",")))


def address_fields(address):
	street = cstr(address.get("street")).strip().split("\n")
	return {
		"address_line1": street[0].strip(),
		"address_line2": ", ".join(s.strip() for s in street[1:] if s.strip()) or None,
		"city": cstr(address.get("city")).strip(),
		"state": cstr(address.get("region")).strip() or None,
		"pincode": cstr(address.get("postcode")).strip() or None,
		"country": country_for(address.get("country")),
	}


def has_content(fields):
	return any(v for k, v in fields.items() if k != "country")


def update_address(row, values):
	values = {k: sanitize_html(v) if isinstance(v, str) else v for k, v in values.items()}
	changed = {k: values[k] for k in UPDATED_ADDRESS_FIELDS if (row.get(k) or None) != (values[k] or None)}
	if changed:
		frappe.db.set_value("Address", row.name, changed)


def address_key(fields):
	return tuple(cstr(fields.get(k)).strip().lower() for k in ("address_line1", "city", "pincode"))


def sync_addresses(doctype, name, title, addresses, authoritative):
	linked = frappe.get_all(
		"Dynamic Link",
		filters={"parenttype": "Address", "link_doctype": doctype, "link_name": name},
		pluck="parent",
	)
	existing = frappe.get_all(
		"Address",
		filters={"name": ("in", linked or [""]), "disabled": 0},
		fields=["name", "address_line1", "city", "pincode", *UPDATED_ADDRESS_FIELDS],
	)
	keys = {address_key(row): row for row in existing}
	wanted = set()
	for address in addresses:
		fields = address_fields(address)
		if not has_content(fields):
			continue
		key = address_key(fields)
		wanted.add(key)
		label = cstr(address.get("type"))
		if key in keys:
			if authoritative and keys[key]:
				update_address(keys[key], {**fields, "carddav_type": label})
			continue
		frappe.get_doc(
			{
				"doctype": "Address",
				"address_title": title,
				"address_type": ADDRESS_TYPES.get(label.split(",")[0], "Other"),
				"carddav_type": label,
				**fields,
				"links": [{"link_doctype": doctype, "link_name": name}],
			}
		).insert(ignore_permissions=True, ignore_mandatory=True)
		keys[key] = None
	if not authoritative:
		return
	for row in existing:
		if address_key(row) not in wanted and only_linked_to(row.name, doctype, name):
			frappe.db.set_value("Address", row.name, "disabled", 1)


def only_linked_to(address, doctype, name):
	links = frappe.get_all(
		"Dynamic Link",
		filters={"parenttype": "Address", "parent": address},
		fields=["link_doctype", "link_name"],
	)
	return all((link.link_doctype, link.link_name) == (doctype, name) for link in links)


def countries():
	cache = run_cache()
	if "countries" not in cache:
		lookup = {}
		for row in frappe.get_all("Country", fields=["name", "code"]):
			for key in (row.name, row.code, frappe._(row.name, lang="de")):
				lookup.setdefault(cstr(key).strip().lower(), row.name)
		for alias, name in COUNTRY_ALIASES.items():
			if name in lookup.values():
				lookup[alias] = name
		cache["countries"] = lookup
	return cache["countries"]


def country_for(text):
	text = cstr(text).strip().lower()
	return countries().get(text) if text else None


def photo_file(doc):
	if not doc.image:
		return None
	return frappe.db.get_value(
		"File",
		{
			"file_url": doc.image,
			"attached_to_doctype": "Contact",
			"attached_to_name": doc.name,
			"file_name": ("like", f"{PHOTO_PREFIX}%"),
		},
		["name", "content_hash"],
		as_dict=True,
	)


def set_photo(doc, card, authoritative):
	if not card.photo and not authoritative:
		return
	current = photo_file(doc)
	if not card.photo and not current:
		return
	digest = get_content_hash(card.photo) if card.photo else None
	if digest and current and current.content_hash == digest:
		return
	if current:
		frappe.delete_doc("File", current.name, ignore_permissions=True)
	url = None
	if card.photo:
		url = (
			frappe.get_doc(
				{
					"doctype": "File",
					"file_name": f"{PHOTO_PREFIX}{frappe.scrub(doc.name)}.{card.photo_type or 'jpg'}",
					"attached_to_doctype": "Contact",
					"attached_to_name": doc.name,
					"attached_to_field": "image",
					"content": card.photo,
					"is_private": 1,
				}
			)
			.insert(ignore_permissions=True)
			.file_url
		)
	if url != doc.image:
		doc.db_set("image", url)


def archive_href(account: str, href: str) -> int:
	names = frappe.get_all(
		"Contact", filters={"carddav_account": account, "carddav_href": href, "archived": 0}, pluck="name"
	)
	for name in names:
		frappe.db.set_value("Contact", name, {"archived": 1, "carddav_etag": None})
		mark_synced(name)
	return len(names)
