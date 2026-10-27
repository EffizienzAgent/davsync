import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import get_datetime, get_system_timezone

from davsync.carddav.client import (
	NS_DECLARATIONS,
	XML_HEADERS,
	CardDAVClient,
	CardDAVError,
	is_collection,
	parse_multistatus,
	prop_text,
	qname,
	xml_body,
)


def is_calendar(entry):
	resourcetype = entry["props"].get(qname("d:resourcetype"))
	return resourcetype is not None and resourcetype.find(qname("cal:calendar")) is not None


def utc_stamp(value):
	local = get_datetime(value).replace(tzinfo=ZoneInfo(get_system_timezone()))
	return local.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


class CalDAVClient(CardDAVClient):
	HOME_SET = "cal:calendar-home-set"
	MULTIGET = "cal:calendar-multiget"
	DATA = "cal:calendar-data"
	MISSING = "Calendar not configured"

	def __init__(self, account: str):
		super().__init__(account)
		values = frappe.db.get_value(
			"CardDAV Account", account, ["calendar_url", "calendar_since"], as_dict=True
		)
		self.calendar_url = values.calendar_url or ""
		self.since = values.calendar_since

	def collection_url(self):
		return self.calendar_url

	def discover_calendar(self, name: str | None = None) -> str:
		home_url, entries = self.collections()
		calendars = [c for c in entries if is_calendar(c)]
		name = (name or "").strip().lower()
		match = [
			c
			for c in calendars
			if not name
			or prop_text(c, "d:displayname").lower() == name
			or c["href"].rstrip("/").rpartition("/")[2].lower() == name
		]
		if not match:
			raise CardDAVError(f"No calendar named {name} found" if name else "No calendar found")
		return urljoin(home_url, match[0]["href"])

	def list_all(self):
		self.require_book()
		token = next(
			(prop_text(e, "d:sync-token") for e in self.propfind(self.calendar_url, "<d:sync-token/>")), ""
		)
		start = f' start="{utc_stamp(self.since)}"' if self.since else ""
		body = xml_body(
			f"<cal:calendar-query {NS_DECLARATIONS}><d:prop><d:getetag/></d:prop>"
			'<cal:filter><cal:comp-filter name="VCALENDAR"><cal:comp-filter name="VEVENT">'
			f'<cal:time-range{start} end="{datetime(2100, 1, 1):%Y%m%dT%H%M%SZ}"/>'
			"</cal:comp-filter></cal:comp-filter></cal:filter></cal:calendar-query>"
		)
		response = self.request("REPORT", self.calendar_url, headers={**XML_HEADERS, "Depth": "1"}, data=body)
		changed = [
			(e["href"], prop_text(e, "d:getetag"))
			for e in parse_multistatus(response)[0]
			if prop_text(e, "d:getetag") and not is_collection(e)
		]
		return changed, [], token, True

	def put(self, *args, **kwargs):
		raise CardDAVError("The calendar is read-only")

	def delete(self, *args, **kwargs):
		raise CardDAVError("The calendar is read-only")

	def put_event(self, href: str, text: str, etag: str | None) -> str:
		return self.put_object(href, text, etag, "text/calendar; charset=utf-8")

	def delete_event(self, href: str, etag: str) -> None:
		CardDAVClient.delete(self, href, etag)

	def supports_scheduling(self) -> bool:
		self.require_book()
		response = self.request("OPTIONS", self.calendar_url)
		return "calendar-auto-schedule" in response.headers.get("DAV", "").lower()

	def free_busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
		home_url, entries = self.collections()
		body = xml_body(
			f"<cal:free-busy-query {NS_DECLARATIONS}>"
			f'<cal:time-range start="{start.astimezone(UTC):%Y%m%dT%H%M%SZ}" '
			f'end="{end.astimezone(UTC):%Y%m%dT%H%M%SZ}"/></cal:free-busy-query>'
		)
		busy = []
		for calendar in (e for e in entries if is_calendar(e)):
			url = urljoin(home_url, calendar["href"])
			try:
				response = self.request("REPORT", url, headers={**XML_HEADERS, "Depth": "1"}, data=body)
			except CardDAVError as exc:
				log_free_busy_failure(url, exc)
				continue
			busy += parse_free_busy(response.content.decode(errors="replace"))
		return merge_intervals(busy)


LOGGED_FAILURES = set()


def log_free_busy_failure(url, exc):
	if url not in LOGGED_FAILURES:
		LOGGED_FAILURES.add(url)
		frappe.log_error(title="CalDAV free/busy failed", message=f"{url}: {exc}")


def parse_stamp(value):
	return datetime.strptime(value.strip(), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def parse_duration(value):
	match = re.fullmatch(
		r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip()
	)
	if not match:
		raise ValueError(value)
	sign, weeks, days, hours, minutes, seconds = match.groups()
	delta = timedelta(
		weeks=int(weeks or 0),
		days=int(days or 0),
		hours=int(hours or 0),
		minutes=int(minutes or 0),
		seconds=int(seconds or 0),
	)
	return -delta if sign == "-" else delta


def parse_free_busy(text):
	lines = re.sub(r"\r?\n[ \t]", "", text).splitlines()
	intervals = []
	for line in lines:
		name, _, value = line.partition(":")
		head, *params = name.split(";")
		if head.upper() != "FREEBUSY" or "FBTYPE=FREE" in (p.upper() for p in params):
			continue
		for period in value.split(","):
			first, _, second = period.partition("/")
			try:
				begin = parse_stamp(first)
				finish = parse_stamp(second) if second.endswith("Z") else begin + parse_duration(second)
			except ValueError:
				continue
			intervals.append((begin, finish))
	return intervals


def merge_intervals(intervals):
	merged = []
	for begin, finish in sorted(intervals):
		if merged and begin <= merged[-1][1]:
			merged[-1] = (merged[-1][0], max(merged[-1][1], finish))
		else:
			merged.append((begin, finish))
	return merged
