import frappe
from frappe.utils import cstr

from davsync.extensions import load


class ContactAdapter:
	"""How contacts relate to the rest of the site. Apps subclass it and name the subclass in the
	`davsync_contact_adapter` hook. The defaults suit a plain Frappe site."""

	link_doctype: str | None = None

	def contact_link(self, card) -> str | None:
		"""The `link_doctype` record a contact from this card belongs to."""
		return None

	def organization_name(self, doc) -> str:
		"""The ORG written to the vCard."""
		return cstr(doc.company_name)

	def company_card(self, card) -> tuple[str | None, str] | None:
		"""Handle a company vCard without a contact; returns (name, outcome) or None to import it as a contact."""
		return None

	def is_shared(self, contact: str) -> bool:
		"""Whether every account sees this contact, not only the account owner's own ones."""
		return False

	def visible_condition(self, user: str) -> str:
		"""SQL on `tabContact` matching the contacts `user`'s address book holds."""
		return f"`tabContact`.`owner` = {frappe.db.escape(user)}"

	def keep_email(self, row) -> bool:
		"""Keep this Contact Email row although the vCard no longer lists it."""
		return False

	def on_adopt(self, doc) -> None:
		"""Called before an existing contact is linked to a vCard for the first time."""


def get_adapter() -> ContactAdapter:
	return load("davsync_contact_adapter", ContactAdapter)
