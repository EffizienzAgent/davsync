CardDAV and CalDAV sync for the Frappe Framework

**davsync** is a Frappe v16 app that keeps the site's contacts in step with a CardDAV address book
and imports a CalDAV calendar. It works with Nextcloud, Radicale, Baïkal, SOGo and other standard
servers.

- **Contacts, two-way.** vCards become Frappe `Contact` records with e-mails, phone numbers,
  addresses, birthday, notes and photo. Changes on either side are synced; when both sides change
  the same card, the server's notes and photo are kept and the site's fields win.
- **Calendar, read-only.** Events with external attendees are imported into Frappe's `Event`.
  Recurring series are expanded 90 days ahead. Nothing is ever written to the calendar.
- **Ignore rules.** Attendee patterns such as `*@newsletter.example.com` and whole series that
  should never be imported.
- **Safety nets.** ETag preconditions on every write, a sync token per account, and a guard that
  refuses to archive or cancel more than 20 records and 30 % of an account after a suspicious
  full listing.

## Installation

```bash
bench get-app https://github.com/EffizienzAgent/davsync
bench --site your.site install-app davsync
```

Requires Frappe v16 and Python 3.14.

## Setup

1. Open **CardDAV Account** and add the server URL, user name and an app password. The account's
   *Owner User* owns imported contacts and events.
2. Click **Find address book** (or enter the address book URL), tick *Enabled*, then
   **Start initial import**. Leave *Push Enabled* on for two-way sync.
3. For the calendar, set *Calendar Name* or leave it empty, click **Calendar → Find calendar**,
   tick *Calendar Sync Enabled* and **Start calendar import**.
4. Optional: **DAV Sync Settings** sets the default country of addresses, the phone region for
   numbers without a country code and your own e-mail addresses, which never count as attendees.

The scheduler syncs every five minutes and refreshes recurring series daily.

See [docs/nextcloud.md](docs/nextcloud.md) for a Nextcloud walk-through and
[docs/extending.md](docs/extending.md) for linking contacts and events to your own doctypes.

## How contacts are matched

- A card that is already linked updates its contact.
- A new card adopts an existing contact with the same e-mail address, then a contact with the same
  name linked to the same record (see `contact_link` in [docs/extending.md](docs/extending.md)).
  Otherwise a new contact is created.
- A card deleted on the server archives the contact (`archived`), it is never deleted.
- Archiving a contact or ticking *Exclude from address book* removes the card from the server.
- Saving a contact pushes it right away. The five-minute run catches up on anything missed and
  picks up new contacts once the first pull has finished.

## Development

```bash
bench --site your.site set-config allow_tests true
bench --site your.site run-tests --app davsync
```

Set `DAVSYNC_TEST_SERVER=http://localhost:5232` (user `test`, password `test`) to run the live
tests against Radicale as well; the CI does this.

## License

MIT, see [LICENSE](LICENSE). Frappe is a trademark of Frappe Technologies; this project is not
affiliated with it.

## Professional setup & support

Need help setting this up or integrating it into your Frappe stack? Write to oss@effizienzagent.de (English or German). German-speaking SMEs: see [mittelstandshub.de](https://mittelstandshub.de).
