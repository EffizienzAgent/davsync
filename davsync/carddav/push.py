import re
import uuid
from contextlib import contextmanager
from itertools import islice
from urllib.parse import urlparse

import frappe
from frappe.utils import add_to_date, cstr, now_datetime, validate_email_address
from frappe.utils.background_jobs import is_job_enqueued

from davsync.carddav import pull, vcard
from davsync.carddav.adapter import get_adapter
from davsync.carddav.client import CardDAVClient, PreconditionFailed
from davsync.settings import default_country

MAX_PUSH_PASSES = 3
MAX_PENDING_PUSHES = 100
ADDRESS_LABELS = {"Personal": "home", "Office": "work"}
BIRTHDAY = re.compile(r"\d{4}-\d{2}-\d{2}|--\d{2}-\d{2}")
JOB = "davsync.carddav.push"
FAILED_BACKOFF_SECONDS = 3600
NEW_CONTACT_DELAY_MINUTES = 10
MASS_DELETE_COUNT = 20
MASS_DELETE_WINDOW_SECONDS = 3600
UNLINK_FIELDS = ("carddav_account", "carddav_href", "carddav_etag", "carddav_raw", "carddav_uid")


def dirty_key(contact):
	return f"carddav-push-dirty-{contact}"


def failed_key(contact):
	return f"carddav-push-failed:{contact}"


def push_job(contact):
	return f"carddav-push-{contact}"


def push_accounts():
	return frappe.get_all(
		"CardDAV Account",
		filters={"enabled": 1, "push_enabled": 1},
		fields=["name", "owner_user", "address_book_url"],
		order_by="creation asc",
	)


def enqueue_contact_push(doc, method=None) -> None:
	if frappe.flags.in_carddav_pull:
		return
	if method == "on_trash":
		enqueue_delete(doc)
	elif push_accounts():
		enqueue_push(doc.name)


def enqueue_push(contact):
	frappe.db.after_commit.add(lambda: frappe.cache.set_value(dirty_key(contact), 1, expires_in_sec=3600))
	frappe.enqueue(
		f"{JOB}.run_push",
		queue="short",
		job_id=push_job(contact),
		deduplicate=True,
		enqueue_after_commit=True,
		contact=contact,
	)


def enqueue_delete(doc):
	if not (doc.carddav_account and doc.carddav_href and doc.carddav_etag):
		return
	frappe.enqueue(
		f"{JOB}.run_delete",
		queue="short",
		job_id=f"carddav-delete-{doc.name}",
		deduplicate=True,
		enqueue_after_commit=True,
		contact=doc.name,
		account=doc.carddav_account,
		href=doc.carddav_href,
		etag=doc.carddav_etag,
	)


def on_address_update(doc, method=None) -> None:
	if frappe.flags.in_carddav_pull or not push_accounts():
		return
	for link in doc.get("links") or []:
		if link.link_doctype == "Contact" and link.link_name:
			enqueue_push(link.link_name)


def log_failure(contact, exc):
	frappe.cache.set_value(failed_key(contact), 1, expires_in_sec=FAILED_BACKOFF_SECONDS)
	frappe.log_error(
		title=f"CardDAV push failed: {contact}",
		message=pull.failure_trace(exc),
		reference_doctype="Contact",
		reference_name=contact,
	)


def run_push(contact) -> None:
	frappe.set_user("Administrator")
	for _attempt in range(MAX_PUSH_PASSES):
		frappe.cache.delete_value(dirty_key(contact))
		try:
			account = account_for(contact)
			if account:
				push_contact(account, contact)
		except Exception as exc:
			frappe.db.rollback()
			log_failure(contact, exc)
			return
		frappe.db.commit()
		if not frappe.cache.get_value(dirty_key(contact)):
			return


def run_delete(contact, account, href, etag) -> None:
	if not frappe.db.get_value("CardDAV Account", account, "enabled") or not frappe.db.get_value(
		"CardDAV Account", account, "push_enabled"
	):
		return
	try:
		delete_card(CardDAVClient(account), account, href, etag)
	except PreconditionFailed:
		log_conflict(contact, href)
	except Exception as exc:
		log_failure(contact, exc)


class MassDeleteBlocked(Exception):
	pass


def delete_card(client, account, href, etag):
	"""Delete one card; never without If-Match, and never more than MASS_DELETE_COUNT an hour."""
	allow_delete(account)
	client.delete(href, etag)


def allow_delete(account):
	key = frappe.cache.make_key(f"carddav-deletes:{account}")
	count = frappe.cache.incrby(key, 1)
	if count == 1:
		frappe.cache.expire(key, MASS_DELETE_WINDOW_SECONDS)
	if count <= MASS_DELETE_COUNT:
		return
	message = frappe._(
		"Push was switched off: more than {0} cards would be deleted within an hour. Check the archived and"
		" deleted contacts, then switch push on again."
	).format(MASS_DELETE_COUNT)
	if frappe.db.get_value("CardDAV Account", account, "push_enabled"):
		frappe.db.set_value("CardDAV Account", account, {"push_enabled": 0, "last_error": message})
		frappe.log_error(
			title="CardDAV mass delete blocked",
			message=message,
			reference_doctype="CardDAV Account",
			reference_name=account,
		)
	raise MassDeleteBlocked(message)


def log_conflict(contact, href):
	frappe.log_error(
		title=f"CardDAV delete conflict: {contact}",
		message=f"{href} changed on the server since the last sync and was kept.",
		reference_doctype="Contact",
		reference_name=contact,
	)


def account_for(contact):
	if not frappe.db.exists("Contact", contact):
		return None
	doc = frappe.get_doc("Contact", contact)
	if doc.carddav_account:
		return doc.carddav_account
	return next((a.name for a in push_accounts() if is_visible(doc, a.owner_user)), None)


def is_visible(doc, user):
	return doc.owner == user or get_adapter().is_shared(doc.name)


def can_push(account):
	settings = frappe.db.get_value(
		"CardDAV Account",
		account,
		["enabled", "push_enabled", "owner_user", "address_book_url"],
		as_dict=True,
	)
	if settings and settings.enabled and settings.push_enabled and settings.address_book_url:
		return settings
	return None


def push_contact(account: str, contact: str, settings=None, client=None) -> str:
	settings = settings or can_push(account)
	if not settings:
		return "skipped"
	doc = frappe.get_doc("Contact", contact)
	if doc.carddav_account and doc.carddav_account != account:
		return "skipped"
	status = sync_card(doc, account, settings, client)
	if status == "skipped":
		store(doc, {"carddav_synced_at": now_datetime()})
	frappe.cache.delete_value(failed_key(contact))
	return status


def sync_card(doc, account, settings, client):
	if doc.exclude_from_address_book:
		return exclude_card(doc, account, client)
	if doc.archived:
		return remove_card(doc, account, client, unlink=False, status="deleted")
	if not is_visible(doc, settings.owner_user):
		if not doc.carddav_href:
			return "skipped"
		store(doc, {**dict.fromkeys(UNLINK_FIELDS), "carddav_synced_at": now_datetime()})
		return "unlinked"
	if not doc.carddav_href:
		current = frappe.db.get_value("Contact", doc.name, UNLINK_FIELDS, as_dict=True, for_update=True)
		if current.carddav_href:
			doc.update(current)
	with country_cache():
		return write_card(client or CardDAVClient(account), account, doc, settings.address_book_url)


def exclude_card(doc, account, client):
	return remove_card(doc, account, client, unlink=True, status="excluded")


def remove_card(doc, account, client, unlink, status):
	deletable = bool(doc.carddav_href and doc.carddav_etag)
	linked = bool(doc.carddav_href or doc.carddav_uid)
	if not deletable and not (unlink and linked):
		return "skipped" if not unlink else status
	if deletable and doc.carddav_account != account:
		return "skipped"
	if deletable:
		client = client or CardDAVClient(account)
		try:
			delete_card(client, account, doc.carddav_href, doc.carddav_etag)
		except PreconditionFailed:
			return keep_changed_card(doc, client)
	cleared = dict.fromkeys(UNLINK_FIELDS) if unlink else {"carddav_etag": None}
	store(doc, {**cleared, "carddav_synced_at": now_datetime()})
	return status


def keep_changed_card(doc, client):
	"""The card changed on the server after our last sync: keep it and let the next pull bring it in."""
	log_conflict(doc.name, doc.carddav_href)
	fetched = client.multiget([doc.carddav_href])
	etag = fetched[0][1] if fetched else None
	store(doc, {"carddav_etag": etag, "carddav_synced_at": now_datetime()})
	return "conflict"


def push_pending(account: str) -> None:
	settings = can_push(account)
	if not settings:
		return
	client = CardDAVClient(account)
	for contact in pending_contacts(account, settings.owner_user):
		try:
			push_contact(account, contact, settings, client)
		except Exception as exc:
			frappe.db.rollback()
			log_failure(contact, exc)
		frappe.db.commit()


def pending_contacts(account, owner):
	rows = frappe.db.sql(
		f"""select name, ifnull(carddav_href, '') = '' as hrefless from `tabContact`
		where (carddav_synced_at is null
			or modified > carddav_synced_at + interval {pull.PENDING_SLACK_SECONDS} second)
		and ((carddav_account = %(account)s and ifnull(carddav_href, '') != '')
			or (%(new_allowed)s and archived = 0 and exclude_from_address_book = 0 and ifnull(carddav_href, '') = ''
				and ifnull(carddav_account, '') in ('', %(account)s)
				and modified < %(cutoff)s
				and {get_adapter().visible_condition(owner)}))
		order by ifnull(carddav_href, '') = '', modified asc""",
		{
			"account": account,
			"new_allowed": int(pull_completed(account)),
			"cutoff": add_to_date(now_datetime(), minutes=-NEW_CONTACT_DELAY_MINUTES),
		},
		as_dict=True,
	)
	due = (
		row.name
		for row in rows
		if not frappe.cache.get_value(failed_key(row.name))
		and not (row.hrefless and is_job_enqueued(push_job(row.name)))
	)
	return list(islice(due, MAX_PENDING_PUSHES))


def pull_completed(account):
	row = frappe.db.get_value("CardDAV Account", account, ["sync_token", "last_error"], as_dict=True)
	return bool(row and row.sync_token and not row.last_error)


def run_push_all(account: str) -> None:
	settings = can_push(account)
	if not settings:
		frappe.db.set_value(
			"CardDAV Account",
			account,
			"last_error",
			frappe._("Push is not possible: enable the account and push, and set the address book URL."),
		)
		return
	client = CardDAVClient(account)
	counts = dict.fromkeys(("created", "updated", "unchanged", "failed"), 0)
	contacts = frappe.get_all(
		"Contact",
		filters={"archived": 0},
		fields=["name", "exclude_from_address_book", "carddav_href"],
		order_by="creation asc",
	)
	for row in contacts:
		if row.exclude_from_address_book and not row.carddav_href:
			continue
		contact = row.name
		try:
			status = push_contact(account, contact, settings, client)
		except Exception as exc:
			frappe.db.rollback()
			log_failure(contact, exc)
			status = "failed"
		if status in counts:
			counts[status] += 1
		frappe.db.commit()
	frappe.db.set_value(
		"CardDAV Account",
		account,
		"last_result",
		frappe._("Created {0}, updated {1}, unchanged {2}, failed {3} contacts on the server.").format(
			*counts.values()
		),
	)


def write_card(client, account, doc, book_url):
	card = card_from_contact(doc)
	href = doc.carddav_href
	if not href:
		new = str(uuid.uuid4()).upper()
		card.uid = card.uid or new
		href = f"{book_path(book_url)}{new}.vcf"
	text = vcard.render(card, doc.carddav_raw or "")
	if doc.carddav_href and doc.carddav_etag and same_card(text, doc.carddav_raw):
		store(doc, {"carddav_synced_at": now_datetime()})
		return "unchanged"
	status = "updated" if doc.carddav_href and doc.carddav_etag else "created"
	try:
		etag = client.put(href, text, doc.carddav_etag if doc.carddav_href else None)
	except PreconditionFailed:
		fetched = client.multiget([href])
		if not fetched:
			raise
		_href, remote_etag, remote_text = fetched[0]
		remote = vcard.parse(remote_text)
		take_remote_only(doc, remote)
		doc.carddav_raw = remote.raw
		card = card_from_contact(doc)
		card.uid = card.uid or remote.uid
		text = vcard.render(card, remote.raw)
		etag = client.put(href, text, remote_etag)
	store(
		doc,
		{
			"carddav_account": account,
			"carddav_uid": card.uid,
			"carddav_href": href,
			"carddav_etag": etag,
			"carddav_raw": vcard.parse(text).raw,
			"carddav_synced_at": now_datetime(),
		},
	)
	return status


def same_card(text, raw):
	return bool(raw) and without_rev(vcard.parse(text).raw) == without_rev(vcard.unfold(raw))


def without_rev(raw):
	return [line for line in vcard.card_lines(raw) if vcard.parse_line(line).name != "REV"]


def take_remote_only(doc, remote):
	store(doc, {"notes": vcard.no_markup(remote.note) or None})
	pull.set_photo(doc, remote, authoritative=True)


def store(doc, values):
	frappe.db.set_value("Contact", doc.name, values, update_modified=False)
	doc.update(values)


def book_path(url):
	path = urlparse(url).path
	return path if path.endswith("/") else f"{path}/"


@contextmanager
def country_cache():
	previous = frappe.flags.carddav_cache
	if previous is None:
		frappe.flags.carddav_cache = {}
	try:
		yield
	finally:
		frappe.flags.carddav_cache = previous


def card_from_contact(doc) -> vcard.Card:
	base = vcard.parse(doc.carddav_raw) if doc.carddav_raw else vcard.Card()
	card = vcard.Card(
		uid=doc.carddav_uid or base.uid,
		prefix=cstr(doc.salutation).strip(),
		suffix=cstr(doc.name_suffix).strip(),
		org=organization_name(doc),
		title=cstr(doc.designation).strip(),
		birthday=birthday(doc, base),
		emails=emails(doc, base),
		phones=phones(doc, base),
		addresses=addresses(doc, base),
	)
	card.given, card.family = names(doc, base)
	if (card.given, card.family, card.prefix, card.suffix) == (
		base.given,
		base.family,
		base.prefix,
		base.suffix,
	):
		card.fn = base.fn
	return card


def names(doc, base):
	given, family = cstr(doc.first_name).strip(), cstr(doc.last_name).strip()
	if not family and not (base.given or base.family) and given in (pull.fallback_name(base), pull.NO_NAME):
		return "", ""
	return given, family


def organization_name(doc):
	return get_adapter().organization_name(doc)


def birthday(doc, base):
	value = cstr(doc.birthday).strip()
	if not value:
		return None
	return value if BIRTHDAY.fullmatch(value) else base.birthday


def emails(doc, base):
	remote = {}
	for address, label in base.emails:
		remote.setdefault(address.strip().lower(), (address, label))
	result = []
	for row in doc.email_ids:
		address = pull.email_of(row)
		if address:
			result.append((remote.get(address, (address,))[0], cstr(row.carddav_type)))
	result += [
		(address, label)
		for address, label in base.emails
		if not validate_email_address(address.strip().lower())
	]
	return result


def phones(doc, base):
	remote = {}
	for raw, label in base.phones:
		raw = raw.strip()
		remote.setdefault(pull.normalized_phone(raw) or raw, (raw, label))
	result = []
	for row in doc.phone_nos:
		phone = cstr(row.phone).strip()
		if phone:
			result.append(
				(remote.get(pull.normalized_phone(phone) or phone, (phone,))[0], cstr(row.carddav_type))
			)
	for line in cstr(doc.phone_unparsed).splitlines():
		line = line.strip()
		if line:
			result.append(remote.get(line, (line, "")))
	return result


def addresses(doc, base):
	linked = frappe.get_all(
		"Dynamic Link",
		filters={"parenttype": "Address", "link_doctype": "Contact", "link_name": doc.name},
		pluck="parent",
	)
	rows = frappe.get_all(
		"Address",
		filters={"name": ("in", linked or [""]), "disabled": 0},
		fields=[
			"address_line1",
			"address_line2",
			"city",
			"state",
			"pincode",
			"country",
			"address_type",
			"carddav_type",
		],
		order_by="creation asc",
	)
	base_remote = [(r, pull.address_fields(r)) for r in base.addresses]
	remote = [r for r in base_remote if pull.has_content(r[1])]
	result = []
	for row in rows:
		fields = crm_fields(row)
		label = cstr(row.carddav_type) or ADDRESS_LABELS.get(row.address_type, "")
		same = next((r for r in remote if fits(r[1], fields)), None)
		if same:
			remote.remove(same)
			result.append({**same[0], "type": label})
			continue
		keyed = next((r for r in remote if pull.address_key(r[1]) == pull.address_key(fields)), None)
		result.append(
			{
				"street": "\n".join(v for v in (fields["address_line1"], fields["address_line2"]) if v),
				"city": fields["city"],
				"region": fields["state"] or "",
				"postcode": fields["pincode"] or "",
				"country": country(row, keyed, base_remote),
				"type": label,
			}
		)
	return result + [r[0] for r in base_remote if not pull.has_content(r[1])]


def fits(remote, fields):
	if remote == fields:
		return True
	unset = not remote["country"] and fields["country"] == default_country()
	return unset and remote == {**fields, "country": remote["country"]}


def country(row, keyed, remote):
	unset = keyed and not keyed[1]["country"] and row.country == default_country()
	if keyed and (not row.country or unset or keyed[1]["country"] == row.country):
		return keyed[0].get("country") or ""
	spelled = next(
		(r[0].get("country") for r in remote if row.country and r[1]["country"] == row.country), None
	)
	if not spelled and row.country == default_country():
		if any(pull.has_content(r[1]) and not r[1]["country"] for r in remote):
			return ""
	return spelled or cstr(row.country)


def crm_fields(row):
	return {
		"address_line1": cstr(row.address_line1).strip(),
		"address_line2": cstr(row.address_line2).strip() or None,
		"city": cstr(row.city).strip(),
		"state": cstr(row.state).strip() or None,
		"pincode": cstr(row.pincode).strip() or None,
		"country": row.country or None,
	}
