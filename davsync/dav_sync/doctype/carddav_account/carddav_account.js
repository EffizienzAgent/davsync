const actions = () => [
	{ label: __("Find address book"), method: "discover" },
	{
		label: __("Find calendar"),
		method: "discover_calendar",
		module: "caldav",
		group: __("Calendar"),
	},
	{
		label: __("Sync calendar now"),
		method: "start_calendar_sync",
		module: "caldav",
		group: __("Calendar"),
		queued: true,
	},
	{
		label: __("Start calendar import"),
		method: "start_calendar_import",
		module: "caldav",
		group: __("Calendar"),
		queued: true,
		confirm: __(
			"Imports the appointments of the calendar since the import date. Nothing is written to the server."
		),
	},
	{ label: __("Sync now"), method: "start_sync", queued: true },
	{
		label: __("Start initial import"),
		method: "start_initial_import",
		queued: true,
		confirm: __("Imports all contacts from the address book."),
	},
	{
		label: __("Send all contacts to the address book"),
		method: "push_all",
		queued: true,
		confirm: __("Sends all contacts of the owner user to the address book. Continue?"),
	},
];

function run(frm, action) {
	const module = action.module || "carddav";
	frappe
		.call(`davsync.${module}.api.${action.method}`, { account: frm.doc.name })
		.then((r) => {
			frm.reload_doc();
			const message = action.queued ? __("Started in the background.") : r.message;
			frappe.show_alert({ message, indicator: "green" });
		});
}

frappe.ui.form.on("CardDAV Account", {
	refresh(frm) {
		if (frm.is_new() || !frappe.user.has_role("System Manager")) return;
		actions().forEach((action) =>
			frm.add_custom_button(
				action.label,
				() =>
					action.confirm
						? frappe.confirm(action.confirm, () => run(frm, action))
						: run(frm, action),
				action.group
			)
		);
	},
});
