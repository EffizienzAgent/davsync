import dataclasses
from pathlib import Path

from frappe.tests import UnitTestCase

from davsync.carddav.vcard import Card, is_company, parse, render

FIXTURES = Path(__file__).parent / "fixtures"
PHOTO = b"\xff\xd8\xff\xe0" + b"synthetic-jpeg-" * 8


def fixture(name):
	return (FIXTURES / name).read_text(encoding="utf-8")


def card(**values):
	base = dict(
		uid="",
		kind="individual",
		given="",
		family="",
		prefix="",
		suffix="",
		fn="",
		org="",
		title="",
		birthday=None,
		emails=[],
		phones=[],
		addresses=[],
		note="",
		photo=None,
		photo_type=None,
		raw="",
	)
	base.update(values)
	return Card(**base)


def without_raw(c):
	return dataclasses.replace(c, raw="")


class TestParse(UnitTestCase):
	def test_parse_apple_card(self):
		c = parse(fixture("apple.vcf"))
		self.assertEqual(c.uid, "4F2A9C1E-0000-4000-8000-00000000A001")
		self.assertEqual(c.kind, "individual")
		self.assertEqual((c.given, c.family, c.prefix, c.suffix), ("Erika", "Mustermann", "Dr.", "MBA"))
		self.assertEqual(c.fn, "Dr. Erika Mustermann")
		self.assertEqual(c.org, "Beispiel, Muster & Co. GmbH")
		self.assertEqual(c.title, "Head of Sales")
		self.assertEqual(c.birthday, "--03-15")
		self.assertEqual(
			c.emails,
			[("erika@example.com", "work,pref"), ("erika.private@example.org", "iCloud")],
		)
		self.assertEqual(
			c.phones,
			[
				("+49 170 1234567", "cell,pref"),
				("+49 40 7654321", "work,fax"),
				("+49 30 1111111", "Notfall"),
			],
		)
		self.assertEqual(
			c.addresses,
			[
				{
					"street": "Musterstraße 1, Hinterhaus",
					"city": "Hamburg",
					"region": "",
					"postcode": "20095",
					"country": "Deutschland",
					"type": "work,pref",
				}
			],
		)
		self.assertEqual(c.note, "Met at the fair\nLikes coffee; no sugar")
		self.assertEqual(c.photo, PHOTO)
		self.assertEqual(c.photo_type, "jpeg")
		self.assertNotIn("\n ", c.raw)

	def test_parse_crlf(self):
		c = parse(fixture("apple.vcf").replace("\n", "\r\n"))
		self.assertEqual(c.photo, PHOTO)
		self.assertEqual(c.addresses[0]["country"], "Deutschland")

	def test_parse_vcard4(self):
		c = parse(fixture("nextcloud4.vcf"))
		self.assertEqual((c.given, c.family), ("Max", "Beispiel"))
		self.assertEqual(c.emails, [("max@example.net", "home")])
		self.assertEqual(c.phones, [("+49-171-2223334", "cell,voice")])
		self.assertEqual(c.addresses[0]["city"], "Lübeck")
		self.assertEqual(c.birthday, "--07-04")
		self.assertEqual(c.photo_type, "png")
		self.assertTrue(c.photo.startswith(b"\x89PNG"))

	def test_parse_birthday_forms(self):
		def bday(line):
			return parse(f"BEGIN:VCARD\nVERSION:3.0\nUID:x\n{line}\nEND:VCARD\n").birthday

		self.assertEqual(bday("BDAY:1980-02-29"), "1980-02-29")
		self.assertEqual(bday("BDAY;TYPE=pref:19800229"), "1980-02-29")
		self.assertEqual(bday("BDAY:1980-02-29T00:00:00Z"), "1980-02-29")
		self.assertEqual(bday("BDAY:--0229"), "--02-29")
		self.assertIsNone(bday("BDAY:sometime"))

	def test_parse_group_kind(self):
		c = parse(fixture("group.vcf"))
		self.assertEqual(c.kind, "group")
		self.assertEqual(c.fn, "Messe 2026")

	def test_parse_company_kind(self):
		self.assertEqual(parse(fixture("company.vcf")).kind, "org")

	def test_photo_uri_not_decoded(self):
		def photo(version, line):
			return parse(f"BEGIN:VCARD\nVERSION:{version}\nUID:x\n{line}\nEND:VCARD\n").photo

		self.assertIsNone(photo("3.0", "PHOTO;VALUE=uri:https://example.test/a.jpg"))
		self.assertIsNone(photo("3.0", "PHOTO:http://example.test/a.jpg"))
		self.assertIsNone(photo("4.0", "PHOTO:https://example.test/a.jpg"))
		self.assertIsNone(photo("4.0", "PHOTO;MEDIATYPE=image/png:aVZCT1J3MEtHZ289"))
		self.assertEqual(photo("3.0", "PHOTO;ENCODING=BASE64;TYPE=PNG:aGFsbG8="), b"hallo")
		self.assertEqual(photo("4.0", "PHOTO:data:image/png;base64,aGFsbG8="), b"hallo")

	def test_photo_uri_line_kept_on_render(self):
		base = "BEGIN:VCARD\nVERSION:4.0\nUID:x\nFN:A\nPHOTO:https://example.test/a.jpg\nEND:VCARD\n"
		c = parse(base)
		c.title = "Neu"
		self.assertIn("PHOTO:https://example.test/a.jpg\r\n", render(c, base))


class TestIsCompany(UnitTestCase):
	def test_is_company_rules(self):
		self.assertTrue(is_company(card(kind="org", fn="X")))
		self.assertTrue(is_company(card(org="Beispiel AG", fn="Beispiel AG")))
		self.assertTrue(is_company(card(org="Beispiel AG", fn="Info")))
		self.assertTrue(is_company(card(given="Max", org="Beispiel AG", fn="Beispiel AG")))
		self.assertFalse(is_company(card(given="Max", family="B", org="Beispiel AG", fn="Max B")))
		self.assertFalse(is_company(card(given="Max", fn="Max")))
		self.assertFalse(is_company(card(fn="")))

	def test_person_with_own_name_as_org_is_not_company(self):
		self.assertFalse(
			is_company(card(given="Erika", family="Muster", org="Erika Muster", fn="Erika Muster"))
		)

	def test_apple_show_as_company(self):
		text = (
			"BEGIN:VCARD\nVERSION:3.0\nUID:x\nN:Lübeck;Finanzamt;;;\nFN:Finanzamt Lübeck\n"
			"ORG:Finanzamt Lübeck\nX-ABSHOWAS:COMPANY\nEND:VCARD\n"
		)
		self.assertTrue(is_company(parse(text)))


class TestRender(UnitTestCase):
	def test_render_preserves_unmanaged(self):
		base = fixture("apple.vcf")
		c = parse(base)
		c.phones[2] = ("+49 30 2222222", "Notfall")
		c.phones.pop(1)
		out = render(c, base)
		self.assertIn(
			"X-SOCIALPROFILE;TYPE=LINKEDIN;TYPE=pref:https://www.linkedin.com/in/example-erika",
			unfold(out),
		)
		self.assertIn("IMPP;TYPE=pref;X-SERVICE-TYPE=Facebook:xmpp:erika.example", out)
		self.assertIn("item4.X-ABLabel:_$!<HomePage>!$_", out)
		self.assertIn("item1.X-ABLabel:iCloud", out)
		self.assertIn("item2.TEL:+49 30 2222222", out)
		self.assertIn("item2.X-ABLabel:Notfall", out)
		self.assertNotIn("7654321", out)
		self.assertIn("CATEGORIES:Kunden,Messe", out)
		self.assertIn("X-CREATED:2024-05-01T10:00:00Z", out)
		self.assertIn("ORG:Beispiel\\, Muster & Co. GmbH;Vertrieb", out)
		self.assertIn("BDAY;X-APPLE-OMIT-YEAR=1604:1604-03-15", out)
		self.assertNotIn("REV:2026-01-01T00:00:00Z", out)
		self.assertEqual(out.count("UID:4F2A9C1E-0000-4000-8000-00000000A001"), 1)
		again = parse(out)
		self.assertEqual(again.phones, [("+49 170 1234567", "cell,pref"), ("+49 30 2222222", "Notfall")])
		self.assertEqual(again.photo, PHOTO)
		self.assertEqual(again.note, c.note)

	def test_render_changes_managed(self):
		base = fixture("apple.vcf")
		c = parse(base)
		c.family = "Musterfrau"
		c.org = "Neue Firma; Teil 2"
		c.birthday = "1981-03-15"
		c.emails.append(("new@example.com", "home"))
		c.phones.append(("+49 1", "Büro Nord"))
		c.addresses[0]["city"] = "Altona"
		c.title = ""
		out = render(c, base)
		self.assertIn("N:Musterfrau;Erika;Maria;Dr.;MBA", out)
		self.assertIn("ORG:Neue Firma\\; Teil 2", out)
		self.assertNotIn("TITLE", out)
		self.assertIn("BDAY:1981-03-15", out)
		self.assertIn("EMAIL;TYPE=HOME;TYPE=INTERNET:new@example.com", out)
		self.assertIn("item5.TEL:+49 1", out)
		self.assertIn("item5.X-ABLabel:Büro Nord", out)
		self.assertIn("item3.ADR;TYPE=WORK;TYPE=pref:;;Musterstraße 1\\, Hinterhaus;Altona;", unfold(out))
		again = parse(out)
		expected = dataclasses.replace(c, raw="")
		self.assertEqual(without_raw(again), expected)

	def test_render_removes_orphan_group_labels(self):
		base = fixture("apple.vcf")
		c = parse(base)
		c.phones = [p for p in c.phones if p[1] != "Notfall"]
		out = render(c, base)
		self.assertNotIn("item2.", out)
		self.assertIn("item1.X-ABLabel:iCloud", out)

	def test_render_new_card_minimal(self):
		c = card(
			uid="new-uid-1",
			given="Max",
			family="Beispiel",
			org="Beispiel AG",
			birthday="--07-04",
			emails=[("max@example.com", "work")],
			phones=[("+49 451 1", "cell")],
			addresses=[
				{
					"street": "Weg 1",
					"city": "Lübeck",
					"region": "",
					"postcode": "23552",
					"country": "Deutschland",
					"type": "home",
				}
			],
			note="ignored",
			photo=PHOTO,
			photo_type="jpeg",
		)
		out = render(c, None)
		lines = out.split("\r\n")
		self.assertEqual(lines[:2], ["BEGIN:VCARD", "VERSION:3.0"])
		self.assertEqual(lines[-2:], ["END:VCARD", ""])
		self.assertIn("UID:new-uid-1", lines)
		self.assertIn("FN:Max Beispiel", lines)
		self.assertIn("N:Beispiel;Max;;;", lines)
		self.assertIn("BDAY;X-APPLE-OMIT-YEAR=1604:1604-07-04", lines)
		self.assertIn("EMAIL;TYPE=WORK;TYPE=INTERNET:max@example.com", lines)
		self.assertIn("TEL;TYPE=CELL:+49 451 1", lines)
		self.assertIn("ADR;TYPE=HOME:;;Weg 1;Lübeck;;23552;Deutschland", lines)
		self.assertTrue(any(line.startswith("REV:") for line in lines))
		self.assertNotIn("NOTE", out)
		self.assertNotIn("PHOTO", out)
		self.assertEqual(parse(out).birthday, "--07-04")

	def test_render_generates_uid(self):
		out = render(card(fn="Someone"), None)
		self.assertTrue(parse(out).uid)

	def test_render_folds_at_75_octets(self):
		c = parse(fixture("apple.vcf"))
		c.note = "x"
		c.title = "Ä" * 60
		out = render(c, fixture("apple.vcf"))
		for line in out.split("\r\n"):
			self.assertLessEqual(len(line.encode("utf-8")), 75)
		self.assertEqual(parse(out).title, "Ä" * 60)
		self.assertEqual(parse(out).photo, PHOTO)

	def test_render_escapes(self):
		out = render(card(uid="u", fn="A", title="a,b;c\\d\ne"), None)
		self.assertIn("TITLE:a\\,b\\;c\\\\d\\ne", out)
		self.assertEqual(parse(out).title, "a,b;c\\d\ne")

	def test_render_vcard4_birthday(self):
		base = fixture("nextcloud4.vcf")
		c = parse(base)
		c.birthday = "--08-01"
		out = render(c, base)
		self.assertIn("BDAY:--0801", out)
		self.assertIn("VERSION:4.0", out)

	def test_roundtrip_stable(self):
		for name in ("apple.vcf", "nextcloud4.vcf", "company.vcf", "group.vcf"):
			base = fixture(name)
			c = parse(base)
			out = render(c, base)
			self.assertEqual(without_raw(parse(out)), without_raw(c), name)
			self.assertEqual(strip_rev(unfold(out)), strip_rev(unfold(base)), name)


def unfold(text):
	return text.replace("\r\n", "\n").replace("\n ", "").replace("\n\t", "")


def strip_rev(text):
	return [line for line in text.split("\n") if line and not line.startswith("REV:")]


def vcard(*lines, version="3.0"):
	return "\n".join(["BEGIN:VCARD", f"VERSION:{version}", "UID:u-1", "FN:Max", *lines, "END:VCARD", ""])


class TestRenderFixes(UnitTestCase):
	def test_render_drops_two_orphan_groups(self):
		base = vcard(
			"item1.EMAIL;TYPE=INTERNET:a@example.com",
			"item1.X-ABLabel:Privat",
			"item2.EMAIL;TYPE=INTERNET:b@example.com",
			"item2.X-ABLabel:Alt",
			"EMAIL;TYPE=WORK:c@example.com",
		)
		c = parse(base)
		c.emails = [("c@example.com", "work")]
		out = render(c, base)
		self.assertNotIn("item1.", out)
		self.assertNotIn("item2.", out)
		self.assertIn("EMAIL;TYPE=WORK:c@example.com", out)

	def test_apple_standard_labels(self):
		base = vcard(
			"item1.TEL:+49 1",
			"item1.X-ABLabel:_$!<Mobile>!$_",
			"item2.TEL:+49 2",
			"item2.X-ABLabel:_$!<HomeFAX>!$_",
			"item3.TEL:+49 3",
			"item3.X-ABLabel:_$!<School>!$_",
		)
		c = parse(base)
		self.assertEqual(c.phones, [("+49 1", "cell"), ("+49 2", "home,fax"), ("+49 3", "School")])
		out = render(c, base)
		self.assertIn("item1.X-ABLabel:_$!<Mobile>!$_", out)
		self.assertNotIn("X-ABLabel:mobile", out)
		c.phones = [("+49 9", "mobile"), ("+49 8", "workfax"), ("+49 7", "cell")]
		out = render(c, base)
		self.assertIn("TEL;TYPE=CELL:+49 9", out)
		self.assertIn("TEL;TYPE=WORK;TYPE=FAX:+49 8", out)
		self.assertIn("item1.TEL:+49 7\r\nitem1.X-ABLabel:_$!<Mobile>!$_", out)
		self.assertNotIn("item2.", out)
		self.assertNotIn("X-ABLabel:mobile", out)
		self.assertEqual(parse(out).phones, [("+49 7", "cell"), ("+49 9", "cell"), ("+49 8", "work,fax")])

	def test_quoted_params_preserved(self):
		adr = (
			'ADR;TYPE=WORK;LABEL="Weg 1, 23552 Lübeck";X-APPLE-SUBLOCALITY=Altstadt;'
			'GEO="geo:53.86,10.68":;;Weg 1;Lübeck;;23552;Deutschland'
		)
		base = vcard(adr, 'TEL;TYPE="cell,voice";VALUE=uri:tel:+49-1', version="4.0")
		c = parse(base)
		self.assertEqual(c.addresses[0]["type"], "work")
		self.assertEqual(c.phones, [("+49-1", "cell,voice")])
		self.assertIn(adr, unfold(render(c, base)))
		c.addresses[0]["street"] = "Weg 2"
		c.phones[0] = ("+49-2", "cell,voice")
		out = unfold(render(c, base))
		self.assertIn(
			'ADR;TYPE=WORK;X-APPLE-SUBLOCALITY=Altstadt;GEO="geo:53.86,10.68":;;Weg 2;Lübeck;;23552;Deutschland',
			out,
		)
		self.assertIn('TEL;TYPE="cell,voice";VALUE=uri:tel:+49-2', out)
		self.assertEqual(parse(out).phones, [("+49-2", "cell,voice")])

	def test_render_fn_fallback(self):
		self.assertIn("FN:Max Beispiel", render(card(uid="u", given="Max", family="Beispiel"), None))
		self.assertIn("FN:Beispiel AG", render(card(uid="u", org="Beispiel AG"), None))
		self.assertIn("FN:a@example.com", render(card(uid="u", emails=[("a@example.com", "")]), None))
		self.assertIn("\r\nFN:", render(card(uid="u"), None))

	def test_org_replacement_keeps_department(self):
		base = vcard("N:Max;;;;", "ORG:Alt GmbH;Vertrieb;Nord")
		c = parse(base)
		c.org = "Neu GmbH"
		out = render(c, base)
		self.assertIn("ORG:Neu GmbH;Vertrieb;Nord", out)
		self.assertEqual(parse(out).org, "Neu GmbH")

	def test_no_empty_n_added_to_base_without_n(self):
		base = vcard("EMAIL:a@example.com", version="4.0")
		c = parse(base)
		out = render(c, base)
		self.assertNotIn("\r\nN:", out)
		self.assertEqual(strip_rev(unfold(out)), strip_rev(unfold(render(parse(out), out))))
		c.given = "Max"
		self.assertIn("N:;Max;;;", render(c, base))
