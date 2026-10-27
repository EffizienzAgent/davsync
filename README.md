CardDAV and CalDAV sync for the Frappe Framework

**davsync** is a Frappe v16 app that keeps the site's contacts in step with a CardDAV address book
and imports a CalDAV calendar. It works with Nextcloud, Radicale, Baïkal, SOGo and other standard
servers.

- **Contacts, two-way.** vCards become Frappe `Contact` records with e-mails, phone numbers,
  addresses, birthday, notes and photo. Changes on either side are synced; when both sides change
  the same card, the server's notes and photo are kept and the site's fields win.
- **Calendar.** Events with external attendees are imported into Frappe's `Event`.
  Recurring series are expanded 90 days ahead. Invitations you declined are left out (or cancelled
  once you decline them). Writing events (and the invitations your server sends for them) is opt-in per account.
- **Ignore rules.** Attendee patterns such as `*@newsletter.example.com` and whole series that
  should never be imported.
- **Safety nets.** ETag preconditions on every write, a sync token per account, and a guard that
  refuses to archive or cancel more than 20 records and 30 % of an account after a suspicious
  full listing. On the push side, more than 20 deletions within an hour switch push off until
  you switch it on again.
- **Hardened against hostile input.** The sync bookkeeping fields cannot be set through the API or
  the desk. Requests only go to the account's server over https, never follow redirects, and only
  touch objects inside the configured address book or calendar. Deletes always carry `If-Match`.
  Recurrence rules are expanded only inside the sync window; rules that never match or need too
  many steps are skipped, logged and reported in the calendar result. XML is
  parsed with defusedxml and responses are capped at 20 MB.

## Installation

```bash
bench get-app https://github.com/EffizienzAgent/davsync --branch version-16
bench --site <site> install-app davsync
```

**Compatibility:** Frappe v16 (Python 3.14). The `version-16` branch always points to the latest release; tagged
releases and the changelog are on the [releases page](https://github.com/EffizienzAgent/davsync/releases).

## Setup

1. Open **CardDAV Account** and add the server URL, user name and an app password. The account's
   *Owner User* owns imported contacts and events.
2. Click **Find address book** (or enter the address book URL), tick *Enabled*, then
   **Start initial import**. Leave *Push Enabled* on for two-way sync.
3. For the calendar, set *Calendar Name* or leave it empty, click **Calendar → Find calendar**,
   tick *Calendar Sync Enabled* and **Start calendar import**.
4. Optional: **DAV Sync Settings** sets the default country of addresses, the phone region for
   numbers without a country code, your own e-mail addresses, which never count as attendees, and
   the sync intervals for contacts and the calendar.

The scheduler syncs every five minutes by default and refreshes recurring series daily. Incremental
runs use sync tokens, so short intervals are cheap.

See [docs/nextcloud.md](docs/nextcloud.md) for a Nextcloud walk-through and
[docs/extending.md](docs/extending.md) for linking contacts and events to your own doctypes.

## How contacts are matched

- A card that is already linked updates its contact.
- A new card adopts an existing contact with the same e-mail address, then a contact with the same
  name linked to the same record (see `contact_link` in [docs/extending.md](docs/extending.md)).
  Otherwise a new contact is created.
- A card deleted on the server archives the contact (`archived`), it is never deleted.
- Archiving a contact or ticking *Exclude from address book* removes the card from the server.
- Saving a contact pushes it right away. The scheduled run catches up on anything missed and
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
