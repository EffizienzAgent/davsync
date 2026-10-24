# Extending davsync

davsync works on a plain Frappe site. An app that has its own idea of companies, sharing or
appointments plugs in through two hooks, each naming a class. The last installed app that sets a
hook wins.

```python
# my_app/hooks.py
required_apps = ["EffizienzAgent/davsync"]

davsync_contact_adapter = "my_app.dav.ContactAdapter"
davsync_calendar_target = "my_app.dav.AppointmentTarget"
```

## Contacts: `davsync_contact_adapter`

Subclass `davsync.carddav.adapter.ContactAdapter` and override what you need.

| Member | Default | Purpose |
| --- | --- | --- |
| `link_doctype` | `None` | Doctype contacts are linked to through `Contact.links`, e.g. `"Customer"`. |
| `contact_link(card)` | `None` | Name of the `link_doctype` record a card belongs to, e.g. found by e-mail domain or `card.org`. New contacts get the link; name matching only adopts contacts linked to the same record. |
| `organization_name(doc)` | `doc.company_name` | The `ORG` written to the vCard. |
| `company_card(card)` | `None` | Handle a company vCard (no person name) yourself and return `(name, outcome)`, outcome being one of `organizations` or `skipped`. `None` imports it as a contact. |
| `is_shared(contact)` | `False` | Whether a contact belongs into every account's address book, not only its owner's. |
| `visible_condition(user)` | owner only | SQL on `` `tabContact` `` that matches the same contacts as `is_shared` plus the user's own. |
| `keep_email(row)` | `False` | Keep a `Contact Email` row although the card no longer lists it. |
| `on_adopt(doc)` | no-op | Called before an existing contact is linked to a card for the first time. |

`card` is a `davsync.carddav.vcard.Card` with `uid`, `given`, `family`, `fn`, `org`, `emails`,
`phones`, `addresses` and more.

```python
import frappe

from davsync.carddav.adapter import ContactAdapter


class CustomerContacts(ContactAdapter):
	link_doctype = "Customer"

	def contact_link(self, card):
		return frappe.db.get_value("Customer", {"customer_name": card.org}) if card.org else None

	def is_shared(self, contact):
		return bool(frappe.db.exists("Dynamic Link", {"parent": contact, "link_doctype": "Customer"}))

	def visible_condition(self, user):
		return (
			f"(`tabContact`.`owner` = {frappe.db.escape(user)} or exists (select 1 from `tabDynamic Link` dl"
			" where dl.parent = `tabContact`.`name` and dl.link_doctype = 'Customer'))"
		)
```

When a linked record is renamed, enqueue a push for its contacts with
`davsync.carddav.push.enqueue_push(contact)` from your own `doc_events`.

## Calendar: `davsync_calendar_target`

By default events go into Frappe's `Event`. To import them into another doctype, subclass
`davsync.caldav.target.CalendarTarget`. The doctype needs the fields `caldav_account`,
`caldav_uid` (unique), `caldav_href`, `caldav_etag` and `status`.

| Method | Purpose |
| --- | --- |
| `upsert(context, href, etag, event, people, name)` | Create or update one record and return `created`, `linked`, `updated` or `skipped`. `event` is a `davsync.caldav.ical.Event`, `people` the attendees that are neither you nor ignored, `name` the existing record or `None`. |
| `cancel(name)` | The event is gone or no longer in the object; mark the record cancelled and return 1 if it changed. |
| `existing`, `find`, `recurring_hrefs`, `series` | Lookups by the `caldav_*` fields; override when your doctype filters further. |
| `is_protected(name)` | Records the ignore rules never hide, e.g. ones another integration owns. |
| `set_ignored(account, uid, ignored)`, `check_series_permission(account, uid)` | Called by **Ignored Calendar Event**. |

`context` carries `account`, `owner`, `tz`, `since`, `until`, `own` (your addresses) and `patterns`
(ignored attendee patterns).

## Settings

**DAV Sync Settings** holds the site-wide defaults. `davsync.settings` reads them:
`default_country()`, `phone_region()` and `self_emails()`. The site config key
`calendar_self_emails` (a list) overrides the own e-mail addresses.

## Flags

While a pull runs, `frappe.flags.in_carddav_pull` is set, and `frappe.flags.in_carddav_import`
during an initial import. Other apps' document hooks can skip their own side effects then.

The `carddav_*` fields on Contact and the `caldav_*` fields on Event are reset to their stored
values on every save, because Frappe does not enforce `read_only` on the server. Code that has to
write them through a document save runs inside `davsync.guard.trusted()`. A custom calendar target
doctype should register `davsync.guard.protect_caldav` as its `validate` hook too; davsync calls
`upsert` and `cancel` inside `trusted()` during a sync.
