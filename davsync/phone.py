import frappe
import phonenumbers
from frappe import _

MIN_LENIENT_DIGITS = 10


def is_acceptable(number) -> bool:
	if phonenumbers.is_valid_number(number):
		return True
	return phonenumbers.is_possible_number(number) and len(str(number.national_number)) >= MIN_LENIENT_DIGITS


def normalize_phone(raw: str, region: str) -> str:
	try:
		number = phonenumbers.parse(raw, region)
	except phonenumbers.NumberParseException:
		number = None
	if number is None or not is_acceptable(number):
		frappe.throw(_("{0} is not a valid phone number.").format(raw))
	return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
