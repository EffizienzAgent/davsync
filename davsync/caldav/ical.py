import copy
import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
from html.parser import HTMLParser
from time import monotonic
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

from davsync.carddav.vcard import no_markup, parse_line, unescape, unfold

DEFAULT_DURATION = timedelta(hours=1)
DESCRIPTION_LIMIT = 10000
SOURCE_LIMIT = 100000
MAX_OCCURRENCES = 500
MAX_ITERATIONS = 50000
MAX_RULES = 5
MAX_COMPONENTS = 1000
MAX_SECONDS = 2
PERIODS = {
	2: timedelta(weeks=1),
	3: timedelta(days=1),
	4: timedelta(hours=1),
	5: timedelta(minutes=1),
	6: timedelta(seconds=1),
}
MONTH_DAYS = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}
LINE_BREAK_TAGS = {"br", "p", "div", "li"}
UID_LENGTH = 200
IGNORED_CUTYPES = {"ROOM", "RESOURCE"}
KEY_FORMAT = "%Y%m%dT%H%M%S"
DURATION = re.compile(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")
GMT_OFFSET = re.compile(r"(?:GMT|UTC)([+-])(\d{2}):?(\d{2})")
UNTIL = re.compile(r"UNTIL=([0-9TZ]+)", re.I)
WINDOWS_ZONES = {
	"W. Europe Standard Time": "Europe/Berlin",
	"Central Europe Standard Time": "Europe/Budapest",
	"Romance Standard Time": "Europe/Paris",
	"GMT Standard Time": "Europe/London",
}


class UnsupportedEvent(ValueError):
	"""An object the sync refuses to expand; it is skipped until it changes."""


@dataclass
class Attendee:
	email: str
	name: str = ""
	partstat: str = ""


@dataclass
class Event:
	uid: str = ""
	key: str = ""
	summary: str = ""
	description: str = ""
	location: str = ""
	url: str = ""
	status: str = ""
	start: datetime | None = None
	end: datetime | None = None
	all_day: bool = False
	recurring: bool = False
	recurrence_id: datetime | None = None
	rules: list[str] = field(default_factory=list)
	exdates: list[datetime] = field(default_factory=list)
	rdates: list[datetime] = field(default_factory=list)
	organizer: Attendee | None = None
	attendees: list[Attendee] = field(default_factory=list)

	@property
	def cancelled(self):
		return self.status == "CANCELLED"


def zone(name, fallback):
	if match := GMT_OFFSET.fullmatch(name):
		sign, hours, minutes = match.groups()
		offset = timedelta(hours=int(hours), minutes=int(minutes))
		return timezone(-offset if sign == "-" else offset)
	name = WINDOWS_ZONES.get(name, name)
	try:
		return ZoneInfo(name) if name else fallback
	except ZoneInfoNotFoundError, ValueError:
		return fallback


def parse_value(value, tzid, tz, is_date=False):
	value = value.strip()
	if is_date or re.fullmatch(r"\d{8}", value):
		return datetime.combine(date(int(value[:4]), int(value[4:6]), int(value[6:8])), datetime.min.time())
	parsed = datetime.strptime(value.rstrip("Zz")[:15], KEY_FORMAT)
	return parsed.replace(tzinfo=UTC if value[-1:] in "Zz" else zone(tzid, tz))


def parse_times(prop, tz):
	tzid = (prop.param("TZID") or [""])[0]
	is_date = (prop.param("VALUE") or [""])[0].upper() == "DATE"
	return [parse_value(v, tzid, tz, is_date) for v in prop.value.split(",") if v.strip()]


def parse_duration(value):
	match = DURATION.fullmatch(value.strip())
	if not match or not any(match.groups()[1:]):
		return None
	sign, weeks, days, hours, minutes, seconds = match.groups()
	delta = timedelta(
		weeks=int(weeks or 0),
		days=int(days or 0),
		hours=int(hours or 0),
		minutes=int(minutes or 0),
		seconds=int(seconds or 0),
	)
	return -delta if sign == "-" else delta


def mail_address(value):
	value = unescape(value).strip()
	if value.lower().startswith("mailto:"):
		value = value[7:]
	value = value.strip().lower()
	return value if "@" in value and " " not in value else ""


def person(prop):
	email = mail_address(prop.value) or mail_address((prop.param("EMAIL") or [""])[0])
	if not email:
		return None
	name = unescape((prop.param("CN") or [""])[0]).strip()
	partstat = (prop.param("PARTSTAT") or [""])[0].upper()
	return Attendee(email, "" if name.lower() == email else name, partstat)


class TextExtractor(HTMLParser):
	def __init__(self):
		super().__init__(convert_charrefs=True)
		self.parts = []

	def handle_starttag(self, tag, attrs):
		if tag == "br":
			self.parts.append("\n")

	def handle_endtag(self, tag):
		if tag in LINE_BREAK_TAGS - {"br"}:
			self.parts.append("\n")

	def handle_data(self, data):
		self.parts.append(data)


def plain_text(value):
	parser = TextExtractor()
	parser.feed(unescape(value)[:SOURCE_LIMIT])
	parser.close()
	text = "".join(parser.parts).replace("\r\n", "\n").strip()
	return re.sub(r"\n{3,}", "\n\n", text)[:DESCRIPTION_LIMIT]


def components(text, name):
	blocks, current, depth = [], None, 0
	for line in unfold(text).split("\n"):
		if not line.strip():
			continue
		upper = line.strip().upper()
		if current is None:
			if upper == f"BEGIN:{name}":
				current, depth = [], 0
			continue
		if upper == f"END:{name}" and depth == 0:
			blocks.append(current)
			current = None
		elif upper.startswith("BEGIN:"):
			depth += 1
		elif upper.startswith("END:"):
			depth -= 1
		elif depth == 0:
			current.append(line)
	return blocks


def parse_event(lines, tz):
	event = Event()
	duration = None
	for prop in map(parse_line, lines):
		if prop.name == "UID":
			event.uid = prop.value.strip()
		elif prop.name == "SUMMARY":
			event.summary = unescape(prop.value).strip()
		elif prop.name == "DESCRIPTION":
			event.description = plain_text(prop.value)
		elif prop.name == "LOCATION":
			event.location = unescape(prop.value).strip()
		elif prop.name == "URL":
			event.url = unescape(prop.value).strip()
		elif prop.name == "STATUS":
			event.status = prop.value.strip().upper()
		elif prop.name == "DTSTART":
			event.start = parse_times(prop, tz)[0]
			event.all_day = event.start.tzinfo is None
		elif prop.name == "DTEND":
			event.end = parse_times(prop, tz)[0]
		elif prop.name == "DURATION":
			duration = parse_duration(prop.value)
		elif prop.name == "RECURRENCE-ID":
			event.recurrence_id = parse_times(prop, tz)[0]
		elif prop.name == "RRULE":
			event.rules.append(prop.value.strip())
		elif prop.name == "EXDATE":
			event.exdates += parse_times(prop, tz)
		elif prop.name == "RDATE" and (prop.param("VALUE") or [""])[0].upper() != "PERIOD":
			event.rdates += parse_times(prop, tz)
		elif prop.name == "ORGANIZER":
			event.organizer = person(prop)
		elif prop.name == "ATTENDEE":
			attendee = person(prop)
			if attendee and (prop.param("CUTYPE") or [""])[0].upper() not in IGNORED_CUTYPES:
				event.attendees.append(attendee)
	for name in ("summary", "description", "location", "url"):
		setattr(event, name, no_markup(getattr(event, name)))
	for attendee in [*event.attendees, *([event.organizer] if event.organizer else [])]:
		attendee.name = no_markup(attendee.name)
	event.recurring = bool(event.rules or event.rdates)
	if event.start and not event.end and duration:
		event.end = event.start + duration
	if event.start and event.end and event.end.tzinfo is None and event.start.tzinfo is not None:
		event.end = event.end.replace(tzinfo=event.start.tzinfo)
	if event.start and event.end and event.start.tzinfo is None and event.end.tzinfo is not None:
		event.end = event.end.replace(tzinfo=None)
	if event.start and (not event.end or event.end <= event.start):
		event.end = event.start + (timedelta(days=1) if event.all_day else DEFAULT_DURATION)
	return event


def local(value, tz):
	if value.tzinfo is None:
		return value
	return value.astimezone(tz).replace(tzinfo=None)


def wall_clock(value, source):
	if value.tzinfo is None or source is None:
		return value.replace(tzinfo=None)
	return value.astimezone(source).replace(tzinfo=None)


def naive_rule(rule, source):
	def until(match):
		raw = match.group(1)
		value = parse_value(f"{raw}T235959" if len(raw) == 8 else raw, "", source)
		return f"UNTIL={wall_clock(value, source).strftime(KEY_FORMAT)}"

	return UNTIL.sub(until, rule)


def expand(master, tz, since, until):
	source = master.start.tzinfo
	start = wall_clock(master.start, source)
	window = (wall_clock(since.replace(tzinfo=tz), source), wall_clock(until.replace(tzinfo=tz), source))
	starts, iterations, started = set(), 0, monotonic()
	if len(master.rules) > MAX_RULES:
		raise UnsupportedEvent(f"More than {MAX_RULES} recurrence rules")
	for text in master.rules:
		for occurrence in windowed(rrulestr(naive_rule(text, source), dtstart=start, cache=False), window):
			iterations += 1
			if iterations > MAX_ITERATIONS:
				raise UnsupportedEvent(f"Recurrence rule {text[:100]} needs too many steps")
			if occurrence > window[1] or len(starts) >= MAX_OCCURRENCES:
				break
			if occurrence >= window[0]:
				starts.add(occurrence)
	if monotonic() - started > MAX_SECONDS:
		raise UnsupportedEvent("Recurrence rules take too long to expand")
	starts |= {
		wall_clock(d, source) for d in master.rdates if window[0] <= wall_clock(d, source) <= window[1]
	}
	if not master.rules and window[0] <= start <= window[1]:
		starts.add(start)
	starts -= {wall_clock(d, source) for d in master.exdates}
	return sorted(s if source is None else s.replace(tzinfo=source) for s in starts)


def windowed(rule, window):
	"""The rule cut to the window: UNTIL at its end and, for fixed-length periods, DTSTART moved by whole
	periods to just before its start, which keeps every occurrence in the window."""
	if never_matches(rule):
		raise UnsupportedEvent("Recurrence rule never matches")
	if rule._count:
		return rule
	changes = {"until": min(rule._until, window[1]) if rule._until else window[1]}
	period = PERIODS.get(rule._freq)
	if period and not rule._bysetpos:
		step = period * rule._interval
		skipped = (window[0] - rule._dtstart) // step - 1
		if skipped > 0:
			changes["dtstart"] = rule._dtstart + skipped * step
	return rule.replace(**changes)


def never_matches(rule):
	months = rule._bymonth or range(1, 13)
	days = rule._bymonthday or ()
	if not days or rule._bynmonthday:
		return False
	return not any(day <= MONTH_DAYS[month] for month in months for day in days)


def uid_key(uid):
	if len(uid) <= UID_LENGTH:
		return uid
	return f"sha1:{hashlib.sha1(uid.encode()).hexdigest()}"


def occurrence_key(uid, original, tz):
	return f"{uid_key(uid)}::{local(original, tz).strftime(KEY_FORMAT)}"


def finalize(event, tz):
	event.start, event.end = local(event.start, tz), local(event.end, tz)
	return event


def occurrences(master, tz, since, until):
	duration = master.end - master.start
	for start in expand(master, tz, since, until):
		event = copy.deepcopy(master)
		event.start, event.end = start, start + duration
		event.key = occurrence_key(master.uid, start, tz)
		yield event


def parse(text, tz, since, until) -> list[Event]:
	"""One event per object; recurring series expand to occurrences between since and until."""
	blocks = components(text, "VEVENT")
	if len(blocks) > MAX_COMPONENTS:
		raise UnsupportedEvent(f"More than {MAX_COMPONENTS} events in one object")
	events = [parse_event(lines, tz) for lines in blocks]
	events = [e for e in events if e.start]
	masters = [e for e in events if not e.recurrence_id]
	overrides = [e for e in events if e.recurrence_id]
	master = masters[0] if masters else None
	if master and not master.recurring:
		master.key = uid_key(master.uid)
		return [finalize(master, tz)]
	result = {e.key: e for e in occurrences(master, tz, since, until)} if master else {}
	for override in overrides:
		override.key = occurrence_key(override.uid, override.recurrence_id, tz)
		override.recurring = True
		if override.key in result or since <= local(override.start, tz) <= until:
			result[override.key] = override
	return [finalize(e, tz) for e in sorted(result.values(), key=lambda e: e.key)]
