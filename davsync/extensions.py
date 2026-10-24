import frappe


def load(hook, default):
	"""The class named by the last app that sets `hook`, else `default`."""
	paths = frappe.get_hooks(hook)
	return frappe.get_attr(paths[-1])() if paths else default()
