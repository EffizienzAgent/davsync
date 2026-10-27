from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from frappe.tests import UnitTestCase

from davsync.caldav import ical
from davsync.caldav.compose import OutgoingEvent, Override, Person, compose

TZ = ZoneInfo("Europe/Berlin")
ORGANIZER = Person("tobi@effizienzagent.de", "Tobias Mende")
GUESTS = [Person("anna@example.com", "Anna"), Person("ben@example.com")]


def event(**changes):
	start = changes.pop("start", datetime(2026, 11, 3, 14, 0))
	values = {
		"uid": "meeting-1@example.com",
		"summary": "Abstimmung",
		"start": start,
		"end": start + timedelta(minutes=30),
		"tz": "Europe/Berlin",
		"organizer": ORGANIZER,
		"attendees": GUESTS,
	}
	return OutgoingEvent(**{**values, **changes})


def parse(text):
	return ical.parse(text, TZ, datetime(2026, 10, 1), datetime(2026, 12, 31))


class TestCompose(UnitTestCase):
	def test_single_event_round_trips(self):
		(parsed,) = parse(compose(event(location="https://zoom.example/j/1", url="https://zoom.example/j/1")))
		self.assertEqual(parsed.start, datetime(2026, 11, 3, 14, 0))
		self.assertEqual(parsed.end, datetime(2026, 11, 3, 14, 30))
		self.assertEqual(parsed.summary, "Abstimmung")
		self.assertEqual(parsed.location, "https://zoom.example/j/1")
		self.assertEqual(parsed.url, "https://zoom.example/j/1")
		self.assertEqual(parsed.organizer.email, "tobi@effizienzagent.de")
		emails = {a.email for a in parsed.attendees}
		self.assertLessEqual({"anna@example.com", "ben@example.com"}, emails)

	def test_series_keeps_local_time_across_dst(self):
		text = compose(event(start=datetime(2026, 10, 20, 13, 45), rrule="FREQ=WEEKLY;INTERVAL=2"))
		starts = [e.start for e in parse(text)]
		self.assertEqual(starts[:2], [datetime(2026, 10, 20, 13, 45), datetime(2026, 11, 3, 13, 45)])

	def test_until_and_overrides(self):
		text = compose(
			event(
				start=datetime(2026, 10, 20, 13, 45),
				rrule="FREQ=WEEKLY;INTERVAL=2",
				until=datetime(2026, 11, 17, 13, 45),
				overrides=[
					Override(
						datetime(2026, 11, 3, 13, 45),
						datetime(2026, 11, 3, 15, 0),
						datetime(2026, 11, 3, 15, 30),
					),
					Override(datetime(2026, 11, 17, 13, 45), None, None, cancelled=True),
				],
			)
		)
		starts = sorted(e.start for e in parse(text))
		self.assertEqual(starts, [datetime(2026, 10, 20, 13, 45), datetime(2026, 11, 3, 15, 0)])

	def test_organizer_is_attendee_accepted_and_no_schedule_agent(self):
		text = compose(event())
		lines = text.replace("\r\n ", "").split("\r\n")
		organizer = next(
			x for x in lines if x.startswith("ATTENDEE") and x.endswith("mailto:tobi@effizienzagent.de")
		)
		self.assertIn("PARTSTAT=ACCEPTED", organizer)
		guest = next(x for x in lines if x.endswith("mailto:anna@example.com"))
		self.assertIn("RSVP=TRUE", guest)
		self.assertNotIn("SCHEDULE-AGENT", text)

	def test_lines_are_folded_and_text_escaped(self):
		description = "Größe, Änderung; Zeile\nzwei " + "Übergrößenmaß " * 20
		text = compose(event(description=description))
		for line in text.split("\r\n"):
			self.assertLessEqual(len(line.encode()), 75)
		for escaped in ("\\,", r"\;", "\\n"):
			self.assertIn(escaped, text.replace("\r\n ", ""))
		(parsed,) = parse(text)
		self.assertEqual(parsed.description, description.strip())

	def test_vtimezone_is_generated_for_the_zone(self):
		text = compose(event())
		self.assertIn("BEGIN:VTIMEZONE\r\nTZID:Europe/Berlin\r\n", text)
		zone = text.split("BEGIN:VTIMEZONE")[1].split("END:VTIMEZONE")[0]
		daylight = zone.split("BEGIN:DAYLIGHT")[1].split("END:DAYLIGHT")[0]
		standard = zone.split("BEGIN:STANDARD")[1].split("END:STANDARD")[0]
		self.assertIn("TZOFFSETFROM:+0100\r\nTZOFFSETTO:+0200", daylight)
		self.assertIn("DTSTART:20260329T020000", daylight)
		self.assertIn("RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU", daylight)
		self.assertIn("TZOFFSETFROM:+0200\r\nTZOFFSETTO:+0100", standard)
		self.assertIn("DTSTART:20261025T030000", standard)
		self.assertIn("RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", standard)
		self.assertIn("DTSTART;TZID=Europe/Berlin:20261103T140000", text)
		self.assertNotRegex(text, r"DTSTART:\d{8}T\d{6}Z")
