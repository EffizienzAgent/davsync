# Nextcloud example

This walk-through connects a Frappe site to the address book and a calendar of one Nextcloud user.
`cloud.example.com` and `alice` stand for your Nextcloud host and user.

## 1. Create an app password

In Nextcloud open **Settings → Security → Devices & sessions**, enter a name such as `Frappe` and
click **Create new app password**. Copy the password; Nextcloud shows it only once. An app password
can be revoked without touching the login password and is not affected by two-factor login.

## 2. Add the account

In the site open **CardDAV Account → New** and fill in:

| Field | Value |
| --- | --- |
| Title | `Nextcloud Alice` |
| Server URL | `https://cloud.example.com/remote.php/dav` |
| Username | `alice` (the login name, not the display name) |
| App Password | the password from step 1 |
| Owner User | the Frappe user whose contacts go into this address book |

Save, then click **Find address book**. davsync skips the read-only books Nextcloud generates
(`z-server-generated`, `z-app-generated`) and prefers one called *Contacts*; the result looks like
`https://cloud.example.com/remote.php/dav/addressbooks/users/alice/contacts/`. To use a different
book, paste its URL into *Address Book URL*. Nextcloud shows it under **Contacts → Settings →
Address books → … → Copy link**.

## 3. First import

Export a backup first: **Contacts → Settings → Address books → … → Download**.

Tick *Enabled* and click **Start initial import**. It runs in the background; *Last Result* shows the
counts. Contacts that already exist on the site with the same e-mail address are adopted instead of
duplicated. While *Push Enabled* is ticked, changes made on the site go back to Nextcloud.

## 4. Calendar

Enter the calendar's name, e.g. `Work`, in *Calendar Name* (or the last part of its URL, e.g.
`work`) and click **Calendar → Find calendar**. The URL looks like
`https://cloud.example.com/remote.php/dav/calendars/alice/work/`. Set *Import From*, tick *Calendar
Sync Enabled* and click **Start calendar import**.

Only events with at least one attendee who is not you are imported. Add your own addresses in **DAV
Sync Settings → Own E-mail Addresses** if you use several, and list mailing-list or room addresses
under *Ignored Attendees*, e.g. `*@lists.example.com`. To drop one series for good, create an
**Ignored Calendar Event** with its UID; deleting that entry brings the events back.

## Troubleshooting

- **HTTP 401**: wrong user name or app password. Nextcloud's login name can differ from the e-mail
  address you log in with.
- **Last Error says a full listing was skipped**: the server suddenly listed far fewer cards than
  known, e.g. after the address book URL changed. Check the URL; nothing was archived.
- **Error Log entries `CardDAV sync failed: …`**: one vCard could not be imported; the others were.
