app_name = "davsync"
app_title = "DAV Sync"
app_publisher = "EffizienzAgent (Tobias Mende)"
app_description = "CardDAV and CalDAV sync for the Frappe Framework"
app_email = "oss@effizienzagent.de"
app_license = "mit"

after_sync = "davsync.install.ensure_indexes"
after_migrate = "davsync.install.ensure_indexes"

fixtures = [{"dt": "Custom Field", "filters": [["module", "=", "DAV Sync"]]}]

doc_events = {
	"Contact": {
		"validate": "davsync.guard.protect_contact",
		"on_update": "davsync.carddav.push.enqueue_contact_push",
		"on_trash": "davsync.carddav.push.enqueue_contact_push",
	},
	"Event": {"validate": "davsync.guard.protect_caldav"},
	"Address": {
		"on_update": "davsync.carddav.push.on_address_update",
		"on_trash": "davsync.carddav.push.on_address_update",
	},
}

scheduler_events = {
	"cron": {
		"*/5 * * * *": [
			"davsync.carddav.pull.pull_all",
			"davsync.caldav.sync.sync_all",
		],
	},
	"daily": ["davsync.caldav.sync.refresh_all"],
}

# Apps change how contacts relate to their own doctypes and where calendar events go by naming a
# subclass of davsync.carddav.adapter.ContactAdapter or davsync.caldav.target.CalendarTarget:
# davsync_contact_adapter = "my_app.dav.ContactAdapter"
# davsync_calendar_target = "my_app.dav.CalendarTarget"
