import base64
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

KNOWN_TYPES = ("home", "work", "cell", "voice", "fax", "main", "iphone", "pager", "pref", "other")
APPLE_LABELS = {
	"mobile": "cell",
	"iphone": "iphone",
	"home": "home",
	"work": "work",
	"main": "main",
	"homefax": "home,fax",
	"workfax": "work,fax",
	"otherfax": "other,fax",
	"pager": "pager",
	"other": "other",
}
APPLE_NO_YEAR = "1604"
LABEL_PROPS = {"X-ABLABEL", "X-ABADR"}
ADR_KEYS = ("street", "city", "region", "postcode", "country")


@dataclass
class Card:
	uid: str = ""
	kind: str = "individual"
	given: str = ""
	family: str = ""
	prefix: str = ""
	suffix: str = ""
	fn: str = ""
	org: str = ""
	title: str = ""
	birthday: str | None = None  # ISO date, or "--MM-DD" without year
	emails: list[tuple[str, str]] = field(default_factory=list)
	phones: list[tuple[str, str]] = field(default_factory=list)
	addresses: list[dict] = field(default_factory=list)
	note: str = ""
	photo: bytes | None = None
	photo_type: str | None = None
	raw: str = ""


@dataclass
class Prop:
	group: str
	name: str
	params: list[tuple[str, str]]
	value: str
	line: str
	head: str

	def param(self, key):
		return [v for k, v in self.params if k == key]


def unfold(text):
	return re.sub(r"\r?\n[ \t]", "", text).replace("\r\n", "\n")


def split_unescaped(value, sep):
	parts, current, i = [], [], 0
	while i < len(value):
		ch = value[i]
		if ch == "\\" and i + 1 < len(value):
			current.append(value[i : i + 2])
			i += 2
			continue
		if ch == sep:
			parts.append("".join(current))
			current = []
		else:
			current.append(ch)
		i += 1
	parts.append("".join(current))
	return parts


def unescape(value):
	return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def escape(value):
	return (
		value.replace("\\", "\\\\")
		.replace(",", "\\,")
		.replace(";", "\\;")
		.replace("\r\n", "\n")
		.replace("\n", "\\n")
	)


def split_outside_quotes(text, sep):
	parts, current, quoted = [], [], False
	for ch in text:
		if ch == '"':
			quoted = not quoted
		if ch == sep and not quoted:
			parts.append("".join(current))
			current = []
		else:
			current.append(ch)
	parts.append("".join(current))
	return parts


def parse_line(line):
	head, quoted = len(line), False
	for i, ch in enumerate(line):
		if ch == '"':
			quoted = not quoted
		elif ch == ":" and not quoted:
			head = i
			break
	parts = split_outside_quotes(line[:head], ";")
	group, _, name = parts[0].rpartition(".")
	params = []
	for part in parts[1:]:
		key, eq, value = part.partition("=")
		if not eq:
			key, value = "TYPE", part
		key = key.upper()
		for item in split_outside_quotes(value, ","):
			item = item.strip('"')
			params.extend((key, v) for v in (item.split(",") if key == "TYPE" else [item]))
	return Prop(group, name.upper(), params, line[head + 1 :], line, line[:head])


def card_lines(raw):
	return [line for line in raw.split("\n") if line.strip()]


def parse_props(raw):
	return [parse_line(line) for line in card_lines(raw)]


def type_label(prop, props):
	if prop.group:
		for other in props:
			if other.group == prop.group and other.name == "X-ABLABEL":
				return apple_label(unescape(other.value))
	labels = []
	for value in prop.param("TYPE"):
		value = value.lower()
		if value in KNOWN_TYPES and value not in labels:
			labels.append(value)
	return ",".join(labels)


def apple_label(label):
	match = re.fullmatch(r"_\$!<(.*)>!\$_", label)
	if not match:
		return label
	return APPLE_LABELS.get(match.group(1).lower(), match.group(1))


def components(value, count):
	items = [unescape(v) for v in split_unescaped(value, ";")]
	return (items + [""] * count)[:count]


def parse_birthday(prop):
	value = prop.value.strip()
	if prop.param("X-APPLE-OMIT-YEAR") and value.startswith(prop.param("X-APPLE-OMIT-YEAR")[0]):
		value = "--" + value[len(prop.param("X-APPLE-OMIT-YEAR")[0]) :].lstrip("-")
	value = value.split("T")[0]
	if match := re.fullmatch(r"(\d{4})-?(\d{2})-?(\d{2})", value):
		return "-".join(match.groups())
	if match := re.fullmatch(r"--(\d{2})-?(\d{2})", value):
		return "--{}-{}".format(*match.groups())
	return None


def parse_phone(prop):
	value = unescape(prop.value)
	if value.lower().startswith("tel:"):
		value = value[4:]
	return value


def parse_address(prop, props):
	parts = components(prop.value, 7)[2:]
	return dict(zip(ADR_KEYS, parts, strict=True), type=type_label(prop, props))


def parse_photo(prop):
	value = prop.value
	if value.startswith("data:"):
		meta, _, value = value[5:].partition(",")
		if ";base64" not in meta.lower():
			return None, None
		photo_type = meta.split(";")[0].rpartition("/")[2]
	elif {v.lower() for v in prop.param("ENCODING")} & {"b", "base64"}:
		photo_type = (prop.param("TYPE") or [""])[0].rpartition("/")[2]
	else:
		return None, None
	try:
		data = base64.b64decode(value, validate=False)
	except ValueError:
		return None, None
	return data, (photo_type.lower() or None)


def parse_kind(props):
	for prop in props:
		value = prop.value.strip().lower()
		if prop.name in ("KIND", "X-ADDRESSBOOKSERVER-KIND") and value == "group":
			return "group"
		if (prop.name == "KIND" and value == "org") or (prop.name == "X-ABSHOWAS" and value == "company"):
			return "org"
	return "individual"


def parse(text):
	raw = unfold(text).strip("\n") + "\n"
	props = parse_props(raw)
	card = Card(raw=raw, kind=parse_kind(props))
	for prop in props:
		if prop.name == "UID":
			card.uid = prop.value.strip()
		elif prop.name == "N":
			card.family, card.given, _, card.prefix, card.suffix = components(prop.value, 5)
		elif prop.name == "FN":
			card.fn = unescape(prop.value)
		elif prop.name == "ORG":
			card.org = components(prop.value, 1)[0]
		elif prop.name == "TITLE":
			card.title = unescape(prop.value)
		elif prop.name == "BDAY":
			card.birthday = parse_birthday(prop)
		elif prop.name == "EMAIL":
			card.emails.append((unescape(prop.value).strip(), type_label(prop, props)))
		elif prop.name == "TEL":
			card.phones.append((parse_phone(prop), type_label(prop, props)))
		elif prop.name == "ADR":
			card.addresses.append(parse_address(prop, props))
		elif prop.name == "NOTE":
			card.note = unescape(prop.value)
		elif prop.name == "PHOTO":
			card.photo, card.photo_type = parse_photo(prop)
	return card


def no_markup(value):
	"""Remote text is plain text: drop angle brackets so it can never become markup."""
	return value.replace("<", "").replace(">", "") if isinstance(value, str) else value


def plain_card(card):
	for name in ("given", "family", "prefix", "suffix", "fn", "org", "title", "note"):
		setattr(card, name, no_markup(getattr(card, name)))
	card.emails = [(no_markup(address), no_markup(label)) for address, label in card.emails]
	card.phones = [(no_markup(number), no_markup(label)) for number, label in card.phones]
	card.addresses = [{key: no_markup(value) for key, value in address.items()} for address in card.addresses]
	return card


def is_company(card):
	if card.kind == "org" or re.search(r"^X-ABSHOWAS:COMPANY\s*$", card.raw, re.M | re.I):
		return True
	if not card.org:
		return False
	return not (card.given or card.family) or (card.fn == card.org and not (card.given and card.family))


def fold(line):
	data = line.encode("utf-8")
	chunks, start, limit = [], 0, 75
	while len(data) - start > limit:
		end = start + limit
		while end > start and (data[end] & 0xC0) == 0x80:
			end -= 1
		chunks.append(data[start:end])
		start, limit = end, 74
	chunks.append(data[start:])
	return "\r\n ".join(chunk.decode("utf-8") for chunk in chunks)


def format_line(group, name, params, value):
	head = f"{group}.{name}" if group else name
	head += "".join(f";{k}={v}" for k, v in params)
	return f"{head}:{value}"


def type_params(label, name, version):
	label = APPLE_LABELS.get(label.lower(), label) if label else ""
	types = [t.lower() for t in label.split(",") if t]
	if any(t not in KNOWN_TYPES for t in types):
		return None
	params = [("TYPE", t if t == "pref" else t.upper()) for t in types if t != "pref"]
	if name == "EMAIL" and version == "3.0":
		params.append(("TYPE", "INTERNET"))
	if "pref" in types:
		params.append(("TYPE", "pref"))
	return params


def head_without(prop, *keys):
	parts = split_outside_quotes(prop.head, ";")
	return ";".join(p for p in parts if p.partition("=")[0].upper() not in keys)


def birthday_line(prop, birthday, version):
	head = head_without(prop, "X-APPLE-OMIT-YEAR", "VALUE") if prop else "BDAY"
	if birthday.startswith("--"):
		month, day = birthday[2:].split("-")
		if version == "3.0":
			return f"{head};X-APPLE-OMIT-YEAR={APPLE_NO_YEAR}:{APPLE_NO_YEAR}-{month}-{day}"
		return f"{head}:--{month}{day}"
	return f"{head}:{birthday}"


def address_value(address, base_value=""):
	head = components(base_value, 2)
	return ";".join(escape(v) for v in head + [address.get(k) or "" for k in ADR_KEYS])


class Renderer:
	def __init__(self, card, base_raw):
		self.card = card
		self.props = parse_props(unfold(base_raw)) if base_raw else []
		self.version = next((p.value.strip() for p in self.props if p.name == "VERSION"), "3.0")
		self.lines = [p.line for p in self.props]
		self.touched_groups = set()
		self.next_item = 1 + max(
			[int(m.group(1)) for p in self.props if (m := re.fullmatch(r"item(\d+)", p.group, re.I))],
			default=0,
		)

	def indexes(self, name):
		return [i for i, p in enumerate(self.props) if p.name == name]

	def insert_at(self, name):
		found = self.indexes(name)
		if not found:
			return self.end_index()
		index = found[-1]
		group = self.props[index].group
		while group and index + 1 < len(self.props) and self.props[index + 1].group == group:
			index += 1
		return index + 1

	def end_index(self):
		ends = self.indexes("END")
		return ends[-1] if ends else len(self.props)

	def insert(self, index, lines):
		for offset, line in enumerate(lines):
			self.props.insert(index + offset, parse_line(line))
			self.lines.insert(index + offset, line)

	def remove(self, index):
		self.touched_groups.add(self.props[index].group)
		self.props[index] = None
		self.lines[index] = None

	def compact(self):
		self.props = [p for p in self.props if p is not None]
		self.lines = [line for line in self.lines if line is not None]

	def single(self, name, parsed, wanted, make_line):
		for i in self.indexes(name)[1:]:
			if parsed(self.props[i]) != wanted:
				self.remove(i)
		self.compact()
		found = self.indexes(name)
		if found and parsed(self.props[found[0]]) == wanted:
			return
		if found:
			index = found[0]
			if wanted in (None, ""):
				self.remove(index)
				self.compact()
				return
			self.lines[index] = make_line(self.props[index])
			self.props[index] = parse_line(self.lines[index])
		elif wanted not in (None, ""):
			self.insert(self.end_index(), [make_line(None)])

	def multi(self, name, wanted, parsed, value_of):
		remaining = list(wanted)
		unmatched = []
		for i in self.indexes(name):
			item = parsed(self.props[i])
			if item in remaining:
				remaining.remove(item)
			else:
				unmatched.append(i)
		dropped = []
		for i in unmatched:
			prop = self.props[i]
			label = label_of(parsed(prop))
			match = next((item for item in remaining if label_of(item) == label), None)
			if match is None:
				dropped.append(i)
				continue
			remaining.remove(match)
			head = head_without(prop, "LABEL") if name == "ADR" else prop.head
			self.lines[i] = f"{head}:{value_of(match, prop)}"
			self.props[i] = parse_line(self.lines[i])
		for i in dropped:
			self.remove(i)
		self.compact()
		new_lines = []
		for item in remaining:
			value = value_of(item, None)
			params = type_params(label_of(item), name, self.version)
			if params is None:
				group = f"item{self.next_item}"
				self.next_item += 1
				new_lines.append(format_line(group, name, [], value))
				new_lines.append(format_line(group, "X-ABLabel", [], escape(label_of(item))))
			else:
				new_lines.append(format_line("", name, params, value))
		if new_lines:
			self.insert(self.insert_at(name), new_lines)

	def drop_orphan_groups(self):
		orphans = {
			group
			for group in self.touched_groups - {""}
			if all(p.name in LABEL_PROPS for p in self.props if p.group == group)
		}
		keep = [i for i, p in enumerate(self.props) if p.group not in orphans]
		self.props = [self.props[i] for i in keep]
		self.lines = [self.lines[i] for i in keep]

	def render(self):
		card = self.card
		new = not self.props
		if new:
			self.start_minimal()
		names = (card.family, card.given, card.prefix, card.suffix)
		if new or any(names) or self.indexes("N"):
			self.single(
				"N",
				lambda p: tuple(components(p.value, 5)[i] for i in (0, 1, 3, 4)),
				names,
				lambda p: plain_raw(
					p,
					"N",
					";".join(
						escape(v)
						for v in (
							card.family,
							card.given,
							components(p.value, 5)[2] if p else "",
							card.prefix,
							card.suffix,
						)
					),
				),
			)
		self.single("FN", lambda p: unescape(p.value), fn_of(card), lambda p: plain(p, "FN", fn_of(card)))
		self.single("ORG", lambda p: components(p.value, 1)[0], card.org, lambda p: org_line(p, card.org))
		self.single("TITLE", lambda p: unescape(p.value), card.title, lambda p: plain(p, "TITLE", card.title))
		self.single(
			"BDAY",
			parse_birthday,
			card.birthday,
			lambda p: birthday_line(p, card.birthday, self.version),
		)
		self.multi(
			"EMAIL",
			card.emails,
			lambda p: (unescape(p.value).strip(), type_label(p, self.props)),
			lambda item, p: escape(item[0]),
		)
		self.multi(
			"TEL",
			card.phones,
			lambda p: (parse_phone(p), type_label(p, self.props)),
			phone_value,
		)
		self.multi(
			"ADR",
			[address_key(a) for a in card.addresses],
			lambda p: address_key(parse_address(p, self.props)),
			lambda item, p: address_value(dict(item), p.value if p else ""),
		)
		self.drop_orphan_groups()
		self.set_rev()
		return "".join(fold(line) + "\r\n" for line in self.lines)

	def start_minimal(self):
		uid = self.card.uid or str(uuid.uuid4()).upper()
		for line in (
			"BEGIN:VCARD",
			"VERSION:3.0",
			"PRODID:-//davsync//CardDAV//EN",
			f"UID:{uid}",
			"END:VCARD",
		):
			self.props.append(parse_line(line))
			self.lines.append(line)

	def set_rev(self):
		line = "REV:" + datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
		found = self.indexes("REV")
		if found:
			self.lines[found[0]] = line
			self.props[found[0]] = parse_line(line)
		else:
			self.insert(self.end_index(), [line])


def label_of(item):
	if isinstance(item, tuple) and item and isinstance(item[0], tuple):
		return dict(item).get("type", "")
	return item[1]


def address_key(address):
	return tuple((k, address.get(k) or "") for k in (*ADR_KEYS, "type"))


def fn_of(card):
	if card.fn:
		return card.fn
	name = " ".join(v for v in (card.prefix, card.given, card.family, card.suffix) if v)
	return name or card.org or next((e for e, _ in card.emails if e), "") or "-"


def plain(prop, name, value):
	return plain_raw(prop, name, escape(value))


def org_line(prop, org):
	rest = split_unescaped(prop.value, ";")[1:] if prop else []
	return plain_raw(prop, "ORG", ";".join([escape(org), *rest]))


def plain_raw(prop, name, value):
	return f"{prop.head if prop else name}:{value}"


def phone_value(item, prop):
	if prop and [v.lower() for v in prop.param("VALUE")] == ["uri"]:
		return "tel:" + item[0]
	return escape(item[0])


def render(card, base_raw):
	return Renderer(card, base_raw).render()
