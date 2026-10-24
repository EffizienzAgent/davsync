import unicodedata

from frappe.utils import cstr


def normalize_person(text):
	text = unicodedata.normalize("NFKD", cstr(text).replace("ß", "ss").replace("ẞ", "ss"))
	return " ".join("".join(c for c in text if not unicodedata.combining(c)).lower().split())


def person_key(first, last):
	parts = (normalize_person(first), normalize_person(last))
	return parts if all(parts) else None
