import frappe

CONTACT_INDEX = "unique_carddav_account_uid"


def ensure_contact_index():
	columns = frappe.db.get_table_columns("Contact")
	if {"carddav_account", "carddav_uid"} <= set(columns):
		frappe.db.add_unique("Contact", ["carddav_account", "carddav_uid"], CONTACT_INDEX)
