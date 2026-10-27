from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

PRODID = "-//davsync//EN"
LINE_LIMIT = 75
STAMP = "%Y%m%dT%H%M%S"
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


@dataclass
class Person:
	email: str
	name: str = ""
	partstat: str = "NEEDS-ACTION"


@dataclass
class Override:
	original: datetime
	start: datetime | None
	end: datetime | None
	cancelled: bool = False


@dataclass
class OutgoingEvent:
	uid: str
	summary: str
	start: datetime
	end: datetime
	tz: str
	organizer: Person
	attendees: list[Person]
	description: str = ""
	location: str = ""
	url: str = ""
	rrule: str = ""
	until: datetime | None = None
	overrides: list[Override] = field(default_factory=list)
	sequence: int = 0


def escape(value):
	value = value.replace("\\", "\\\\").replace(";", r"\;").replace(",", "\\,")
	return value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")


def quoted(value):
	return '"' + value.replace('"', "'") + '"'


def fold(line):
	parts, current, size = [], "", 0
	for char in line:
		width = len(char.encode())
		if size + width > LINE_LIMIT - (1 if parts else 0):
			parts.append(current)
			current, size = "", 0
		current += char
		size += width
	return "\r\n ".join([*parts, current])


def transitions(zone, year):
	"""(instant, offset before, offset after) for every UTC offset change in the year."""
	hour, end = datetime(year, 1, 1, tzinfo=UTC), datetime(year + 1, 1, 1, tzinfo=UTC)
	result, before = [], hour.astimezone(zone).utcoffset()
	while hour < end:
		hour += timedelta(hours=1)
		after = hour.astimezone(zone).utcoffset()
		if after != before:
			result.append((hour, before, after))
		before = after
	return result


def offset(delta):
	total = int(delta.total_seconds())
	sign = "-" if total < 0 else "+"
	return f"{sign}{abs(total) // 3600:02d}{abs(total) // 60 % 60:02d}"


def timezone_lines(name, year):
	zone = ZoneInfo(name)
	lines = ["BEGIN:VTIMEZONE", f"TZID:{name}"]
	changes = transitions(zone, year)
	if not changes:
		current = datetime(year, 1, 1, tzinfo=UTC).astimezone(zone).utcoffset()
		changes = [(datetime(year, 1, 1, tzinfo=UTC), current, current)]
	for instant, before, after in changes:
		local = (instant + before).replace(tzinfo=None)
		kind = "DAYLIGHT" if after > before else "STANDARD"
		lines += [f"BEGIN:{kind}", f"TZOFFSETFROM:{offset(before)}", f"TZOFFSETTO:{offset(after)}"]
		lines.append(f"DTSTART:{local.strftime(STAMP)}")
		if len(changes) > 1:
			week = -1 if (local + timedelta(days=7)).month != local.month else (local.day - 1) // 7 + 1
			lines.append(f"RRULE:FREQ=YEARLY;BYMONTH={local.month};BYDAY={week}{WEEKDAYS[local.weekday()]}")
		lines.append(f"END:{kind}")
	return [*lines, "END:VTIMEZONE"]


def person_line(name, who, extra=()):
	params = [*([f"CN={quoted(who.name)}"] if who.name else []), *extra]
	return f"{name}{''.join(f';{p}' for p in params)}:mailto:{who.email}"


def compose(event: OutgoingEvent) -> str:
	zone = ZoneInfo(event.tz)

	def at(name, value):
		return f"{name};TZID={event.tz}:{value.strftime(STAMP)}"

	def people():
		yield person_line("ATTENDEE", event.organizer, ["PARTSTAT=ACCEPTED", "ROLE=REQ-PARTICIPANT"])
		for guest in event.attendees:
			if guest.email.lower() != event.organizer.email.lower():
				yield person_line(
					"ATTENDEE", guest, [f"PARTSTAT={guest.partstat}", "ROLE=REQ-PARTICIPANT", "RSVP=TRUE"]
				)

	def vevent(start, end, extra=()):
		lines = ["BEGIN:VEVENT", f"UID:{event.uid}", f"DTSTAMP:{datetime.now(UTC).strftime(STAMP)}Z"]
		lines += [*extra, at("DTSTART", start), at("DTEND", end), f"SUMMARY:{escape(event.summary)}"]
		lines += [
			f"{key}:{escape(value)}"
			for key, value in (("DESCRIPTION", event.description), ("LOCATION", event.location))
			if value
		]
		lines += [f"URL:{event.url}"] if event.url else []
		lines += [f"SEQUENCE:{event.sequence}", "STATUS:CONFIRMED", person_line("ORGANIZER", event.organizer)]
		return [*lines, *people(), "END:VEVENT"]

	extra = []
	if event.rrule:
		rule = event.rrule
		if event.until:
			rule += f";UNTIL={event.until.replace(tzinfo=zone).astimezone(UTC).strftime(STAMP)}Z"
		extra.append(f"RRULE:{rule}")
	extra += [at("EXDATE", o.original) for o in event.overrides if o.cancelled]
	master = vevent(event.start, event.end, extra)
	duration = event.end - event.start
	moved = [o for o in event.overrides if not o.cancelled]
	lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN"]
	lines += [*timezone_lines(event.tz, event.start.year), *master]
	for o in moved:
		start = o.start or o.original
		lines += vevent(start, o.end or start + duration, [at("RECURRENCE-ID", o.original)])
	lines.append("END:VCALENDAR")
	return "".join(fold(line) + "\r\n" for line in lines)
