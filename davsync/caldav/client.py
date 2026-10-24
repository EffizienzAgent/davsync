from datetime import UTC, datetime
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
