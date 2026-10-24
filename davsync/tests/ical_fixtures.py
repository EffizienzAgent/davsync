def vevent(uid, start="20260715T100000", end="20260715T110000", tzid="Europe/Berlin", extra=(), attendees=()):
	lines = ["BEGIN:VEVENT", f"UID:{uid}", "SUMMARY:Abstimmung", "DTSTAMP:20260701T080000Z"]
	lines.append(f"DTSTART;TZID={tzid}:{start}" if tzid else f"DTSTART:{start}")
	if end:
		lines.append(f"DTEND;TZID={tzid}:{end}" if tzid else f"DTEND:{end}")
	lines += [f'ATTENDEE;CN="{name}";PARTSTAT=ACCEPTED:mailto:{email}' for email, name in attendees]
	lines += list(extra)
	lines.append("END:VEVENT")
	return lines


def calendar(*events):
	lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Test//EN"]
	lines += [
		"BEGIN:VTIMEZONE",
		"TZID:Europe/Berlin",
		"BEGIN:DAYLIGHT",
		"RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
		"DTSTART:19700329T020000",
		"END:DAYLIGHT",
		"END:VTIMEZONE",
	]
	for event in events:
		lines += event
	lines.append("END:VCALENDAR")
	return "\r\n".join(lines) + "\r\n"
