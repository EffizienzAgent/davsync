import frappe

CONTACT_INDEX = "unique_carddav_account_uid"
EVENT_INDEX = "unique_caldav_account_uid"
OLD_EVENT_INDEX = "caldav_uid"


def ensure_indexes():
	ensure_unique("Contact", ("carddav_account", "carddav_uid"), CONTACT_INDEX)
	ensure_unique("Event", ("caldav_account", "caldav_uid"), EVENT_INDEX)
	if frappe.db.has_index("tabEvent", OLD_EVENT_INDEX):
		frappe.db.sql_ddl(f"alter table `tabEvent` drop index `{OLD_EVENT_INDEX}`")


def ensure_unique(doctype, fields, name):
	if set(fields) <= set(frappe.db.get_table_columns(doctype)):
		frappe.db.add_unique(doctype, list(fields), name)
