from collections import Counter
from zoneinfo import ZoneInfo

import frappe
from frappe import _
from frappe.utils import add_days, cstr, get_datetime, get_system_timezone, now_datetime, today

from davsync.caldav import ical
from davsync.caldav.client import CalDAVClient
from davsync.caldav.ignores import (
	attendee_patterns,
	ignored_uids,
	is_ignored_attendee,
	normalize,
	series_uid,
)
from davsync.caldav.target import get_target
from davsync.carddav.pull import failure_trace, is_mass_archive, job_running
from davsync.guard import trusted
from davsync.settings import self_emails as configured_self_emails
from davsync.settings import sync_due, sync_interval

SAVEPOINT = "caldav_object"
RUN_SAVEPOINT = "caldav_run"
HORIZON_DAYS = 90
COUNTS = ("created", "linked", "updated", "cancelled", "skipped", "failed")
LONG_JOBS = ("calendar-sync", "calendar-import")
TITLE_LENGTH = 140
LOCATION_LENGTH = 140
URL_LENGTH = 1000


class Context:
	def __init__(self, account):
		values = frappe.db.get_value(
			"CardDAV Account", account, ["owner_user", "calendar_since"], as_dict=True
		)
		self.account = account
		self.owner = values.owner_user
		self.tz = ZoneInfo(get_system_timezone())
		self.since = get_datetime(values.calendar_since or "2000-01-01")
		self.until = get_datetime(add_days(today(), HORIZON_DAYS))
		self.own = self_emails(values.owner_user)
		self.patterns = attendee_patterns(account)
		self.ignored = ignored_uids(account)


def self_emails(owner):
	own = {normalize(e) for e in configured_self_emails()}
	if owner:
		own.add(normalize(frappe.db.get_value("User", owner, "email") or owner))
	return own - {""}


def calendar_accounts():
	return frappe.get_all(
		"CardDAV Account",
		filters={"calendar_enabled": 1, "calendar_url": ("is", "set")},
		pluck="name",
		order_by="creation asc",
	)


def sync_all() -> None:
	if not sync_due("calendar"):
		return
	for account in calendar_accounts():
		if not job_running(account, LONG_JOBS):
			run_pull(account)
			frappe.db.commit()


def refresh_all() -> None:
	for account in calendar_accounts():
		if not job_running(account, LONG_JOBS):
			run_pull(account, refresh=True)
			frappe.db.commit()


def run_pull(account, initial=False, refresh=False):
	frappe.db.savepoint(RUN_SAVEPOINT)
	try:
		counts = refresh_recurring(account) if refresh else pull(account, initial)
	except Exception as exc:
		frappe.db.rollback(save_point=RUN_SAVEPOINT)
		record_error(account, exc)
		return False
	frappe.db.set_value("CardDAV Account", account, "calendar_last_result", result_summary(counts))
	return True


def record_error(account, exc):
	from frappe.utils.password import get_decrypted_password

	frappe.log_error(
		title="Calendar sync failed",
		message=failure_trace(exc),
		reference_doctype="CardDAV Account",
		reference_name=account,
	)
	message = f"{type(exc).__name__}: {exc}"
	secret = get_decrypted_password("CardDAV Account", account, "app_password", raise_exception=False)
	if secret:
		message = message.replace(secret, "***")
	frappe.db.set_value("CardDAV Account", account, "calendar_last_error", message[:500])


def result_summary(counts):
	summary = _("Created {0}, linked {1}, updated {2}, cancelled {3}, skipped {4}, failed {5}.").format(
		*(counts[key] for key in COUNTS)
	)
	if counts["limited"]:
		summary += " " + _("{0} objects exceed the limits for recurring events; see Error Log.").format(
			counts["limited"]
		)
	return summary


def known_objects(account):
	rows = frappe.get_all(
		"Calendar Object", filters={"account": account}, fields=["href", "etag", "failed", "skipped"]
	)
	return {row.href: row for row in rows}


def failed_hrefs(account):
	return frappe.get_all("Calendar Object", filters={"account": account, "failed": 1}, pluck="href")


def stale_hrefs(account):
	"""Objects whose etag was cleared (e.g. a series shown again); fetched on the next sync."""
	return frappe.get_all(
		"Calendar Object", filters={"account": account, "etag": ("is", "not set")}, pluck="href"
	)


def remember(account, href, etag, failed=False, error=None):
	values = {"etag": etag, "failed": int(failed), "skipped": int(bool(error)), "error": error}
	name = frappe.db.get_value("Calendar Object", {"account": account, "href": href})
	if name:
		frappe.db.set_value("Calendar Object", name, values)
	else:
		frappe.get_doc({"doctype": "Calendar Object", "account": account, "href": href, **values}).insert(
			ignore_permissions=True
		)


def forget(account, href):
	frappe.db.delete("Calendar Object", {"account": account, "href": href})


def needs_fetch(href, etag, known, fresh, full):
	row = known.get(href)
	if row and row.skipped and etag and row.etag == etag:
		return False
	if fresh or not etag or not row:
		return True
	return row.etag != etag or bool(full and row.failed)


def pull(account: str, initial: bool = False) -> Counter:
	client = CalDAVClient(account)
	settings = frappe.db.get_value(
		"CardDAV Account",
		account,
		["calendar_sync_token", "calendar_fresh", "calendar_url_changed"],
		as_dict=True,
	)
	fresh = initial or bool(settings.calendar_fresh)
	changed, deleted, new_token, full = client.sync(None if fresh else settings.calendar_sync_token or None)
	known = known_objects(account)
	etags = dict(changed)
	todo = [href for href, etag in changed if needs_fetch(href, etag, known, fresh, full)]
	counts = Counter(skipped=len(changed) - len(todo))
	gone_now = set(deleted)
	stale = [h for h in stale_hrefs(account) if h not in etags and h not in gone_now]
	todo += stale
	context = Context(account)
	fetched = set()
	for href, etag, text in client.multiget(todo) if todo else []:
		fetched.add(href)
		counts.update(pull_one(context, href, etag or etags.get(href), text))
	gone = set(deleted) | (set(stale) - fetched)
	blocking, notes = [], []
	if full:
		missing = {href for href in known if href not in etags}
		if settings.calendar_url_changed:
			for href in missing:
				forget(account, href)
			if missing:
				notes.append(
					_("Calendar changed: {0} appointments of the previous calendar were kept.").format(
						len(missing)
					)
				)
		elif is_mass_archive(len(missing), len(known)):
			blocking.append(_("Full listing would cancel {0} appointments — skipped.").format(len(missing)))
		else:
			gone |= missing
	for href in gone:
		counts["cancelled"] += cancel_href(account, href)
		forget(account, href)
	if counts["failed"]:
		notes.append(_("{0} calendar objects could not be synced; see Error Log.").format(counts["failed"]))
	values = {"calendar_last_error": " ".join(blocking + notes)}
	if not blocking:
		values.update(
			calendar_sync_token=new_token,
			calendar_last_sync=now_datetime(),
			calendar_fresh=0,
			calendar_url_changed=0,
		)
	frappe.db.set_value("CardDAV Account", account, values)
	return counts


def refresh_recurring(account):
	hrefs = get_target().recurring_hrefs(account)
	return refetch(account, [*hrefs, *failed_hrefs(account)])


def refetch(account, hrefs):
	hrefs = list(dict.fromkeys(hrefs))
	counts = Counter()
	context = Context(account)
	for href, etag, text in CalDAVClient(account).multiget(hrefs) if hrefs else []:
		counts.update(pull_one(context, href, etag, text))
	return counts


def pull_one(context, href, etag, text):
	frappe.db.savepoint(SAVEPOINT)
	try:
		events = ical.parse(text, context.tz, context.since, context.until)
		with trusted():
			outcomes = apply_object(context, href, etag, events)
	except ical.UnsupportedEvent as exc:
		frappe.db.rollback(save_point=SAVEPOINT)
		frappe.log_error(
			title=f"CalDAV event skipped: {href}",
			message=str(exc),
			reference_doctype="CardDAV Account",
			reference_name=context.account,
		)
		remember(context.account, href, etag, error=str(exc)[:500])
		return Counter(limited=1)
	except Exception as exc:
		frappe.db.rollback(save_point=SAVEPOINT)
		frappe.clear_messages()
		previous = frappe.db.get_value(
			"Calendar Object", {"account": context.account, "href": href}, ["etag", "failed"], as_dict=True
		)
		if not (previous and previous.failed and previous.etag == etag):
			frappe.log_error(
				title=f"CalDAV event failed: {href}",
				message=failure_trace(exc),
				reference_doctype="CardDAV Account",
				reference_name=context.account,
			)
		remember(context.account, href, etag, failed=True)
		return Counter(failed=1)
	remember(context.account, href, etag)
	frappe.db.release_savepoint(SAVEPOINT)
	return outcomes


def apply_object(context, href, etag, events):
	target = get_target()
	existing = target.existing(context.account, href)
	outcomes, seen = Counter(), set()
	for event in events:
		name = existing.get(event.key) or target.find(context.account, event.key)
		if series_uid(event.key) in context.ignored and not (name and target.is_protected(name)):
			seen.add(event.key)
			outcomes["skipped"] += 1
			continue
		people = participants_of(event, context)
		if name:
			seen.add(event.key)
		elif not people or event.cancelled or event.start < context.since:
			outcomes["skipped"] += 1
			continue
		outcomes[target.upsert(context, href, etag, event, people or None, name)] += 1
	for key, name in existing.items():
		if key not in seen:
			outcomes["cancelled"] += target.cancel(name)
	return outcomes


def participants_of(event, context):
	people = {}
	for person in [*event.attendees, *([event.organizer] if event.organizer else [])]:
		if person.email not in context.own and not is_ignored_attendee(person.email, context.patterns):
			people.setdefault(person.email, person)
	return list(people.values())


def fit(value, length):
	return cstr(value).strip()[:length]


def meeting_link(event):
	for value in (event.url, event.location):
		if cstr(value).lower().startswith(("https://", "http://")) and len(value) <= URL_LENGTH:
			return value
	return ""


def cancel_href(account, href):
	target = get_target()
	return sum(target.cancel(name) for name in target.existing(account, href).values())


def request_refetch(account, hrefs):
	"""Clear the etags so the next calendar sync fetches the objects again."""
	for href in dict.fromkeys(hrefs):
		remember(account, href, None)


def refetch_note(account):
	if frappe.db.get_value("CardDAV Account", account, "calendar_enabled"):
		return _("The calendar data is refreshed with the next calendar sync (within {0} min).").format(
			sync_interval("calendar")
		)
	return _("The calendar sync is disabled; the calendar data is refreshed once it is enabled.")
