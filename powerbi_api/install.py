import frappe


def after_install():
	"""Create the role that grants access to the Power BI endpoints."""
	if not frappe.db.exists("Role", "Power BI Reader"):
		frappe.get_doc({"doctype": "Role", "role_name": "Power BI Reader", "desk_access": 0}).insert(
			ignore_permissions=True
		)
		frappe.db.commit()
