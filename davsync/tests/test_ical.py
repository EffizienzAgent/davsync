from datetime import datetime
from zoneinfo import ZoneInfo

from frappe.tests import IntegrationTestCase

from davsync.caldav import ical
from davsync.tests.ical_fixtures import calendar, vevent

TZ = ZoneInfo("Europe/Berlin")
SINCE = datetime(2026, 7, 1)
UNTIL = datetime(2026, 12, 31)


def parse(*events):
	return ical.parse(calendar(*events), TZ, SINCE, UNTIL)


class TestICal(IntegrationTestCase):
	def test_timezones_convert_to_system_time(self):
		[berlin] = parse(vevent("a"))
		[utc] = parse(vevent("b", "20260715T080000Z", "20260715T090000Z", tzid=None))
		[vienna] = parse(vevent("c", tzid="Europe/Vienna"))
		[bangkok] = parse(vevent("d", tzid="Asia/Bangkok"))
		[offset] = parse(vevent("e", tzid="GMT+0300"))
		self.assertEqual(berlin.start, datetime(2026, 7, 15, 10, 0))
		self.assertEqual(utc.start, datetime(2026, 7, 15, 10, 0))
		self.assertEqual(vienna.start, datetime(2026, 7, 15, 10, 0))
		self.assertEqual(bangkok.start, datetime(2026, 7, 15, 5, 0))
		self.assertEqual(offset.start, datetime(2026, 7, 15, 9, 0))
		self.assertEqual(berlin.end, datetime(2026, 7, 15, 11, 0))

	def test_unknown_timezone_falls_back_to_system_time(self):
		[event] = parse(vevent("a", tzid="Mars/Olympus"))
		self.assertEqual(event.start, datetime(2026, 7, 15, 10, 0))

	def test_all_day_event(self):
		[event] = parse(
			[
				"BEGIN:VEVENT",
				"UID:day",
				"DTSTART;VALUE=DATE:20260720",
				"DTEND;VALUE=DATE:20260722",
				"END:VEVENT",
			]
		)
		self.assertTrue(event.all_day)
		self.assertEqual((event.start, event.end), (datetime(2026, 7, 20), datetime(2026, 7, 22)))

	def test_missing_end_uses_duration_or_default(self):
		[duration] = parse(vevent("a", end=None, extra=["DURATION:PT30M"]))
		[default] = parse(vevent("b", end=None))
		self.assertEqual(duration.end, datetime(2026, 7, 15, 10, 30))
		self.assertEqual(default.end, datetime(2026, 7, 15, 11, 0))

	def test_attendees_organizer_and_properties(self):
		[event] = parse(
			vevent(
				"a",
				attendees=[("anna@firma.example", "Anna Muster")],
				extra=[
					"ORGANIZER;CN=Alice:mailto:alice@example.com",
					"ATTENDEE;CUTYPE=ROOM;CN=Raum:mailto:raum@firma.example",
					"ATTENDEE;PARTSTAT=DECLINED:MAILTO:Bert@Firma.Example",
					"LOCATION:https://meet.example/abc",
					"STATUS:CANCELLED",
					"DESCRIPTION:Zeile 1\\nZeile\\, 2 <b>fett</b>",
				],
			)
		)
		self.assertEqual(
			[(a.email, a.name, a.partstat) for a in event.attendees],
			[("anna@firma.example", "Anna Muster", "ACCEPTED"), ("bert@firma.example", "", "DECLINED")],
		)
		self.assertEqual(event.organizer.email, "alice@example.com")
		self.assertTrue(event.cancelled)
		self.assertEqual(event.location, "https://meet.example/abc")
		self.assertEqual(event.description, "Zeile 1\nZeile, 2 fett")

	def test_folded_lines(self):
		text = calendar(vevent("a", extra=["SUMMARY:Lange", " Zeile"])).replace("SUMMARY:Abstimmung\r\n", "")
		[event] = ical.parse(text, TZ, SINCE, UNTIL)
		self.assertEqual(event.summary, "LangeZeile")

	def test_description_is_capped(self):
		[event] = parse(vevent("a", extra=["DESCRIPTION:" + "x" * 20000]))
		self.assertEqual(len(event.description), ical.DESCRIPTION_LIMIT)

	def test_weekly_series_expands_with_exdate_and_override(self):
		series = vevent(
			"w",
			"20260601T090000",
			"20260601T093000",
			extra=[
				"RRULE:FREQ=WEEKLY;UNTIL=20260731T220000Z",
				"EXDATE;TZID=Europe/Berlin:20260713T090000",
			],
		)
		moved = vevent(
			"w",
			"20260721T150000",
			"20260721T160000",
			extra=["RECURRENCE-ID;TZID=Europe/Berlin:20260720T090000"],
		)
		events = parse(series, moved)
		self.assertEqual(
			[e.start for e in events],
			[
				datetime(2026, 7, 6, 9),
				datetime(2026, 7, 21, 15),
				datetime(2026, 7, 27, 9),
			],
		)
		self.assertEqual(events[0].key, "w::20260706T090000")
		self.assertEqual(events[1].key, "w::20260720T090000")
		self.assertTrue(all(e.recurring for e in events))
		self.assertEqual(events[0].end, datetime(2026, 7, 6, 9, 30))

	def test_series_keeps_wall_clock_across_timezones(self):
		events = parse(
			vevent(
				"b",
				"20261020T150000",
				"20261020T160000",
				tzid="Asia/Bangkok",
				extra=["RRULE:FREQ=WEEKLY;COUNT=2"],
			)
		)
		self.assertEqual([e.start for e in events], [datetime(2026, 10, 20, 10), datetime(2026, 10, 27, 9)])

	def test_single_override_without_master(self):
		[event] = parse(vevent("o", extra=["RECURRENCE-ID;TZID=Europe/Berlin:20260715T100000"]))
		self.assertEqual(event.key, "o::20260715T100000")

	def test_date_start_with_timezone_end(self):
		[event] = parse(
			[
				"BEGIN:VEVENT",
				"UID:mix",
				"DTSTART;VALUE=DATE:20260720",
				"DTEND;TZID=Europe/Berlin:20260721T000000",
				"END:VEVENT",
			]
		)
		self.assertEqual((event.start, event.end), (datetime(2026, 7, 20), datetime(2026, 7, 21)))

	def test_long_uid_is_hashed(self):
		long_uid = "x" * 300
		[single] = parse(vevent(long_uid))
		[occurrence] = parse(vevent(long_uid, extra=["RRULE:FREQ=DAILY;COUNT=1"]))
		self.assertTrue(single.key.startswith("sha1:"))
		self.assertLessEqual(len(occurrence.key), 255)
		self.assertEqual(single.uid, long_uid)

	def test_rdate_series_applies_exdate_to_dtstart(self):
		events = parse(
			vevent(
				"r",
				extra=[
					"RDATE;TZID=Europe/Berlin:20260722T100000",
					"EXDATE;TZID=Europe/Berlin:20260715T100000",
				],
			)
		)
		self.assertEqual([e.start for e in events], [datetime(2026, 7, 22, 10)])
