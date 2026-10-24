import uuid
from unittest.mock import patch

import frappe

from davsync.carddav import pull, push


def tag():
	return uuid.uuid4().hex[:10]


def forbid(*args, **kwargs):
	raise AssertionError("frappe.db.commit called in a test without patching it")


def guard_commit(case):
	patcher = patch.object(frappe.db, "commit", side_effect=forbid)
	patcher.start()
	case.addCleanup(patcher.stop)


def only_accounts(case, *names):
	push_accounts, enabled_accounts = push.push_accounts, pull.enabled_accounts
	for module, attr, replacement in (
		(push, "push_accounts", lambda: [a for a in push_accounts() if a.name in names]),
		(pull, "enabled_accounts", lambda: [a for a in enabled_accounts() if a in names]),
	):
		patcher = patch.object(module, attr, replacement)
		patcher.start()
		case.addCleanup(patcher.stop)


def settle_contacts():
	frappe.db.sql(
		"update `tabContact` set carddav_synced_at = modified"
		" where carddav_synced_at is null or carddav_synced_at < modified"
	)


def use_hooks(case, **hooks):
	real = frappe.get_hooks

	def get_hooks(hook=None, *args, **kwargs):
		if hook in hooks:
			return list(hooks[hook])
		return real(hook, *args, **kwargs)

	patcher = patch.object(frappe, "get_hooks", get_hooks)
	patcher.start()
	case.addCleanup(patcher.stop)


def make_carddav_account(**fields):
	return frappe.get_doc(
		{
			"doctype": "CardDAV Account",
			"title": f"CardDAV Account {tag()}",
			"server_url": "https://cloud.example.test/remote.php/dav",
			"username": "alice",
			"app_password": "app-pw-1234",
			"owner_user": "Administrator",
			**fields,
		}
	).insert()


def make_user(email, roles=()):
	if frappe.db.exists("User", email):
		return email
	user = frappe.get_doc({"doctype": "User", "email": email, "first_name": email.partition("@")[0]})
	user.flags.no_welcome_mail = True
	user.insert(ignore_permissions=True)
	if roles:
		user.add_roles(*roles)
	return email


def make_contact(first_name, last_name="", emails=(), phones=(), **fields):
	doc = frappe.get_doc(
		{
			"doctype": "Contact",
			"first_name": first_name,
			"last_name": last_name,
			"email_ids": [{"email_id": e, "is_primary": int(i == 0)} for i, e in enumerate(emails)],
			"phone_nos": [{"phone": p} for p in phones],
			**fields,
		}
	)
	return doc.insert(ignore_permissions=True)
