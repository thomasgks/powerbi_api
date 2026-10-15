"""Power BI data API.

All endpoints are plain Frappe whitelisted methods:

    GET /api/method/powerbi_api.api.sales
    GET /api/method/powerbi_api.api.transfers
    GET /api/method/powerbi_api.api.adjustments
    GET /api/method/powerbi_api.api.inventory
    GET /api/method/powerbi_api.api.stock_by_store

Auth: ``Authorization: token <api_key>:<api_secret>`` of a user holding the
"Power BI Reader" (or System Manager) role.

Common query params:
    from_date, to_date   YYYY-MM-DD, filter on posting date (sales/transfers/adjustments)
    limit                rows per page (default 10000, max 50000)
    offset               rows to skip
    docstatus            comma list, default "1" (submitted). e.g. "0,1,2"

Response (inside Frappe's {"message": ...} envelope):
    {"data": [...], "count": n, "offset": o, "limit": l, "has_more": bool}
"""

import re

import frappe
from frappe.utils import cint, flt, getdate

from powerbi_api.helpers import get_item_map, item_block, split_group

ROLES = ("Power BI Reader", "System Manager")
MAX_LIMIT = 50000
DEFAULT_LIMIT = 10000

# Which Sales Invoice column carries the Animo reference. Change here if the
# reference is stored elsewhere. POS Invoices use custom_billno.
SALES_INVOICE_ANIMO_FIELD = "po_no"
POS_INVOICE_ANIMO_FIELD = "custom_billno"

DOCSTATUS_LABEL = {0: "Draft", 1: "Submitted", 2: "Cancelled"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _check():
	frappe.only_for(ROLES)


def _page(limit, offset):
	limit = min(max(cint(limit) or DEFAULT_LIMIT, 1), MAX_LIMIT)
	offset = max(cint(offset), 0)
	return limit, offset


def _dates(from_date, to_date):
	return (
		getdate(from_date) if from_date else getdate("1900-01-01"),
		getdate(to_date) if to_date else getdate("2999-12-31"),
	)


def _docstatus(value):
	try:
		vals = tuple(sorted({int(v) for v in str(value or "1").split(",") if v.strip() != ""}))
	except ValueError:
		frappe.throw("docstatus must be a comma separated list of 0, 1, 2")
	vals = tuple(v for v in vals if v in (0, 1, 2))
	return vals or (1,)


def _envelope(rows, limit, offset):
	has_more = len(rows) > limit
	rows = rows[:limit]
	return {"data": rows, "count": len(rows), "offset": offset, "limit": limit, "has_more": has_more}


def _addr(html):
	if not html:
		return ""
	text = re.sub(r"<br\s*/?>", ", ", html)
	text = re.sub(r"<[^>]+>", "", text)
	return re.sub(r"(,\s*)+$", "", text.replace("\n", " ")).strip()


# --------------------------------------------------------------------------
# Sales
# --------------------------------------------------------------------------
def _sales_select(dt, animo_field, store_expr, cost_expr, extra_where):
	return f"""
		SELECT p.name AS receipt_no, p.posting_date, p.status, {store_expr} AS store,
			p.{animo_field} AS animo_ref, p.customer_name, p.contact_mobile, p.address_display,
			p.is_return, p.total_taxes_and_charges AS doc_tax, p.net_total AS doc_net_total,
			i.idx, i.item_code, i.qty, i.net_amount, i.discount_amount, i.distributed_discount_amount,
			i.pricing_rules, {cost_expr} AS cost, '{dt}' AS doctype
		FROM `tab{dt}` p
		JOIN `tab{dt} Item` i ON i.parent = p.name
		WHERE p.docstatus IN %(docstatus)s
			AND p.posting_date BETWEEN %(from_date)s AND %(to_date)s
			{extra_where}
	"""


@frappe.whitelist()
def sales(from_date=None, to_date=None, limit=None, offset=None, docstatus=None):
	"""One row per invoice line from POS Invoice + Sales Invoice.

	Consolidated Sales Invoices (created from POS closing) are skipped because
	their lines already exist as POS Invoices, so nothing is counted twice.
	"""
	_check()
	limit, offset = _page(limit, offset)
	fd, td = _dates(from_date, to_date)

	pos = _sales_select(
		"POS Invoice", POS_INVOICE_ANIMO_FIELD, "COALESCE(NULLIF(i.warehouse,''), p.set_warehouse)", "0", ""
	)
	inv = _sales_select(
		"Sales Invoice",
		SALES_INVOICE_ANIMO_FIELD,
		"COALESCE(NULLIF(p.custom_store,''), NULLIF(i.warehouse,''), p.set_warehouse)",
		"i.incoming_rate",
		"AND p.is_consolidated = 0",
	)
	rows = frappe.db.sql(
		f"""
		SELECT * FROM ({pos} UNION ALL {inv}) t
		ORDER BY posting_date, receipt_no, idx
		LIMIT %(lim)s OFFSET %(offset)s
		""",
		{
			"docstatus": _docstatus(docstatus),
			"from_date": fd,
			"to_date": td,
			"lim": limit + 1,
			"offset": offset,
		},
		as_dict=True,
	)
	page = rows[:limit]

	items = get_item_map({r.item_code for r in page})
	pay = _payment_types({(r.doctype, r.receipt_no) for r in page})

	out = []
	for r in page:
		m = items.get(r.item_code, {})
		g = item_block(m)
		net = flt(r.net_amount)
		tax = flt(r.doc_tax) * net / flt(r.doc_net_total) if flt(r.doc_net_total) else 0
		discount = flt(r.discount_amount) * flt(r.qty) + flt(r.distributed_discount_amount)
		cost = flt(r.cost) or flt(m.get("valuation_rate"))
		out.append(
			{
				"Receipt #": r.receipt_no,
				"Bill date": r.posting_date,
				"Bill status": r.status,
				"Store code": r.store,
				"Animo Reference": r.animo_ref,
				"VC": g["vc"],
				"Vendor Name": g["vendor_name"],
				"Brand": g["brand"],
				"DCS": g["dcs"],
				"Dept.": g["dept"],
				"Sub Group": g["sub_group"],
				"ALU": r.item_code,
				"Barcode": g["barcode"],
				"Style": g["style"],
				"Item Name": g["item_name"],
				"Size": g["size"],
				"Season": g["season"],
				"Original Price": g["original_price"],
				"Supplier": g["supplier"],
				"Color": g["color"],
				"Color Code": g["color_code"],
				"Qty": flt(r.qty),
				"Tax": flt(tax, 4),
				"Sales w/o tax": flt(net, 4),
				"Price Level 1": g["price_level_1"],
				"Price Level 2": g["price_level_2"],
				"Price Level 3": g["price_level_3"],
				"Cost": flt(cost, 4),
				"Discount": flt(discount, 4),
				"Customer name": r.customer_name,
				"Mobile no.": r.contact_mobile,
				"Bill address": _addr(r.address_display),
				"Payment type": pay.get((r.doctype, r.receipt_no), ""),
				"Promo code": r.pricing_rules or "",
			}
		)
	return _envelope(out + rows[limit:], limit, offset)


def _payment_types(keys):
	if not keys:
		return {}
	names = tuple({k[1] for k in keys})
	rows = frappe.db.sql(
		"""
		SELECT parenttype, parent, mode_of_payment
		FROM `tabSales Invoice Payment`
		WHERE parent IN %(names)s AND parenttype IN ('POS Invoice','Sales Invoice')
			AND amount != 0
		ORDER BY idx
		""",
		{"names": names},
		as_dict=True,
	)
	res = {}
	for r in rows:
		cur = res.setdefault((r.parenttype, r.parent), [])
		if r.mode_of_payment not in cur:
			cur.append(r.mode_of_payment)
	return {k: ", ".join(v) for k, v in res.items()}


# --------------------------------------------------------------------------
# Transfers
# --------------------------------------------------------------------------
_SLE_AFTER = """(SELECT l.qty_after_transaction FROM `tabStock Ledger Entry` l
	WHERE l.voucher_no = {v}.name AND l.voucher_detail_no = d.name
		AND l.warehouse = {wh} AND l.is_cancelled = 0
	ORDER BY l.creation DESC LIMIT 1)"""

_STATUS_SQL = """CASE se.docstatus WHEN 0 THEN 'Draft' WHEN 2 THEN 'Cancelled'
	ELSE CASE WHEN se.add_to_transit = 1 AND IFNULL(se.per_transferred,0) < 100 THEN 'In Transit'
		WHEN se.add_to_transit = 1 THEN 'Received' ELSE 'Submitted' END END"""


@frappe.whitelist()
def transfers(from_date=None, to_date=None, limit=None, offset=None, docstatus=None):
	"""One row per Stock Entry line with purpose 'Material Transfer'.

	from/to store OH = stock on hand in that warehouse right after the entry
	(from the Stock Ledger), not today's balance.
	"""
	_check()
	limit, offset = _page(limit, offset)
	fd, td = _dates(from_date, to_date)

	from_wh = "COALESCE(NULLIF(d.s_warehouse,''), se.from_warehouse)"
	to_wh = "COALESCE(NULLIF(d.t_warehouse,''), se.to_warehouse)"
	rows = frappe.db.sql(
		f"""
		SELECT se.name AS slip_erp, se.custom_doc_no AS slip_animo, {_STATUS_SQL} AS status,
			se.posting_date, {from_wh} AS from_store, {to_wh} AS to_store,
			d.item_code, d.transfer_qty AS qty, d.basic_rate AS cost, d.idx,
			{_SLE_AFTER.format(v="se", wh=from_wh)} AS from_oh,
			{_SLE_AFTER.format(v="se", wh=to_wh)} AS to_oh
		FROM `tabStock Entry` se
		JOIN `tabStock Entry Detail` d ON d.parent = se.name
		WHERE se.purpose = 'Material Transfer' AND se.docstatus IN %(docstatus)s
			AND se.posting_date BETWEEN %(from_date)s AND %(to_date)s
		ORDER BY se.posting_date, se.name, d.idx
		LIMIT %(lim)s OFFSET %(offset)s
		""",
		{
			"docstatus": _docstatus(docstatus),
			"from_date": fd,
			"to_date": td,
			"lim": limit + 1,
			"offset": offset,
		},
		as_dict=True,
	)
	page = rows[:limit]
	items = get_item_map({r.item_code for r in page})

	out = []
	for r in page:
		g = item_block(items.get(r.item_code, {}))
		out.append(
			{
				"From Store": r.from_store,
				"To Store": r.to_store,
				"Slip# Animo": r.slip_animo,
				"Slip# ERP": r.slip_erp,
				"Status": r.status,
				"Transfer date": r.posting_date,
				"VC": g["vc"],
				"Vendor Name": g["vendor_name"],
				"Brand": g["brand"],
				"DCS": g["dcs"],
				"ALU": r.item_code,
				"Barcode": g["barcode"],
				"Style": g["style"],
				"Item Name": g["item_name"],
				"Size": g["size"],
				"Season": g["season"],
				"Original Price": g["original_price"],
				"Supplier": g["supplier"],
				"Color": g["color"],
				"Color Code": g["color_code"],
				"Qty": flt(r.qty),
				"Cost": flt(r.cost, 4),
				"Price Level 1": g["price_level_1"],
				"Price Level 2": g["price_level_2"],
				"Price Level 3": g["price_level_3"],
				"Tax": 0,
				"Dept.": g["dept"],
				"Sub Group": g["sub_group"],
				"From store OH": None if r.from_oh is None else flt(r.from_oh),
				"To store Stock OH": None if r.to_oh is None else flt(r.to_oh),
			}
		)
	return _envelope(out + rows[limit:], limit, offset)


# --------------------------------------------------------------------------
# Adjustments
# --------------------------------------------------------------------------
@frappe.whitelist()
def adjustments(from_date=None, to_date=None, limit=None, offset=None, docstatus=None):
	"""Stock adjustments: Material Receipt / Material Issue entries and Stock Reconciliations.

	OH Qty = quantity on hand before the adjustment; adj qty = signed change.
	"""
	_check()
	limit, offset = _page(limit, offset)
	fd, td = _dates(from_date, to_date)

	se_store = "COALESCE(NULLIF(d.t_warehouse,''), NULLIF(d.s_warehouse,''))"
	se_after = _SLE_AFTER.format(v="se", wh=se_store)
	se_adj = "IF(IFNULL(d.t_warehouse,'') != '', d.transfer_qty, -d.transfer_qty)"
	status = "CASE {t}.docstatus WHEN 0 THEN 'Draft' WHEN 1 THEN 'Submitted' ELSE 'Cancelled' END"

	rows = frappe.db.sql(
		f"""
		SELECT * FROM (
			SELECT se.name AS slip_erp, se.custom_doc_no AS slip_animo, {status.format(t='se')} AS status,
				se.posting_date, {se_store} AS store, d.item_code,
				{se_after} - {se_adj} AS oh_qty, {se_adj} AS adj_qty, d.basic_rate AS cost,
				IF(se.is_opening = 'Yes', {se_adj}, NULL) AS opening_stock,
				se.purpose AS adj_type, d.idx
			FROM `tabStock Entry` se
			JOIN `tabStock Entry Detail` d ON d.parent = se.name
			WHERE se.purpose IN ('Material Receipt','Material Issue') AND se.docstatus IN %(docstatus)s
				AND se.posting_date BETWEEN %(from_date)s AND %(to_date)s
			UNION ALL
			SELECT sr.name, NULL, {status.format(t='sr')}, sr.posting_date, i.warehouse, i.item_code,
				i.current_qty, IFNULL(i.qty,0) - IFNULL(i.current_qty,0), i.valuation_rate,
				IF(sr.purpose = 'Opening Stock', i.qty, NULL), 'Stock Reconciliation', i.idx
			FROM `tabStock Reconciliation` sr
			JOIN `tabStock Reconciliation Item` i ON i.parent = sr.name
			WHERE sr.docstatus IN %(docstatus)s
				AND sr.posting_date BETWEEN %(from_date)s AND %(to_date)s
		) t
		ORDER BY posting_date, slip_erp, idx
		LIMIT %(lim)s OFFSET %(offset)s
		""",
		{
			"docstatus": _docstatus(docstatus),
			"from_date": fd,
			"to_date": td,
			"lim": limit + 1,
			"offset": offset,
		},
		as_dict=True,
	)
	page = rows[:limit]
	items = get_item_map({r.item_code for r in page})

	out = []
	for r in page:
		g = item_block(items.get(r.item_code, {}))
		out.append(
			{
				"Store code": r.store,
				"Slip# Animo": r.slip_animo,
				"Slip# ERP": r.slip_erp,
				"Status": r.status,
				"Adjustment date": r.posting_date,
				"Adjustment type": r.adj_type,
				"VC": g["vc"],
				"Vendor Name": g["vendor_name"],
				"Brand": g["brand"],
				"DCS": g["dcs"],
				"ALU": r.item_code,
				"Barcode": g["barcode"],
				"Style": g["style"],
				"Item Name": g["item_name"],
				"Size": g["size"],
				"Season": g["season"],
				"Original Price": g["original_price"],
				"Supplier": g["supplier"],
				"Color": g["color"],
				"Color Code": g["color_code"],
				"OH Qty": None if r.oh_qty is None else flt(r.oh_qty),
				"adj qty": flt(r.adj_qty),
				"Cost": flt(r.cost, 4),
				"Price Level 1": g["price_level_1"],
				"Price Level 2": g["price_level_2"],
				"Price Level 3": g["price_level_3"],
				"Tax": 0,
				"Dept.": g["dept"],
				"Sub Group": g["sub_group"],
				"Opening Stock": None if r.opening_stock is None else flt(r.opening_stock),
			}
		)
	return _envelope(out + rows[limit:], limit, offset)


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------
@frappe.whitelist()
def inventory(limit=None, offset=None, warehouse=None, include_disabled=0, include_templates=0):
	"""Item master, one row per barcode (items without a barcode appear once).

	QTY OH / po_qty are totals across all warehouses, or for `warehouse` if given.
	po_price is the rate on the most recent submitted Purchase Order line.
	"""
	_check()
	limit, offset = _page(limit, offset)
	cond = ""
	if not cint(include_disabled):
		cond += " AND i.disabled = 0"
	if not cint(include_templates):
		cond += " AND i.has_variants = 0"

	rows = frappe.db.sql(
		f"""
		SELECT i.name AS item_code, bc.barcode
		FROM `tabItem` i
		LEFT JOIN `tabItem Barcode` bc ON bc.parent = i.name AND bc.parenttype = 'Item'
		WHERE 1 = 1 {cond}
		ORDER BY i.name, bc.idx
		LIMIT %(lim)s OFFSET %(offset)s
		""",
		{"lim": limit + 1, "offset": offset},
		as_dict=True,
	)
	page = rows[:limit]
	codes = {r.item_code for r in page}
	items = get_item_map(codes)
	bins = _bin_totals(codes, warehouse)
	po = _last_po_rate(codes)

	out = []
	for r in page:
		m = items.get(r.item_code, {})
		g = item_block(m)
		l1, l2, l3 = split_group(m.get("item_group"))
		b = bins.get(r.item_code, (0, 0))
		out.append(
			{
				"brand": m.get("brand"),
				"Group-Level1": l1,
				"Group-Level2": l2,
				"Group-Level3": l3,
				"item_code": r.item_code,
				"variant_of": m.get("variant_of"),
				"custom_style_code": m.get("custom_style_code"),
				"item_name": m.get("item_name"),
				"custom_item_name_ar": m.get("custom_item_name_ar"),
				"description": m.get("description"),
				"Color": g["color"],
				"Color Code": g["color_code"],
				"Size": g["size"],
				"Season": g["season"],
				"Assortment": g["assortment"],
				"barcode": r.barcode,
				"custom_material": m.get("custom_material"),
				"custom_image_url": m.get("custom_image_url"),
				"custom_bin_no": m.get("custom_bin_no"),
				"custom_dcs": m.get("custom_dcs"),
				"custom_vendor_code": m.get("custom_vendor_code"),
				"custom_vendor_name": m.get("custom_vendor_name"),
				"valuation_rate": flt(m.get("valuation_rate"), 4),
				"PriceLevel3": g["price_level_3"],
				"PriceLevel1": g["price_level_1"],
				"PriceLevel2": g["price_level_2"],
				"is_stock_item": cint(m.get("is_stock_item")),
				"po_qty": flt(b[1]),
				"po_price": po.get(r.item_code),
				"QTY OH": flt(b[0]),
			}
		)
	return _envelope(out + rows[limit:], limit, offset)


def _bin_totals(codes, warehouse=None):
	res = {}
	codes = list(codes)
	for i in range(0, len(codes), 2000):
		wh = "AND warehouse = %(wh)s" if warehouse else ""
		for r in frappe.db.sql(
			f"""SELECT item_code, SUM(actual_qty) AS oh, SUM(ordered_qty) AS po
			FROM `tabBin` WHERE item_code IN %(codes)s {wh} GROUP BY item_code""",
			{"codes": tuple(codes[i : i + 2000]), "wh": warehouse},
			as_dict=True,
		):
			res[r.item_code] = (r.oh, r.po)
	return res


def _last_po_rate(codes):
	res = {}
	codes = list(codes)
	for i in range(0, len(codes), 2000):
		for r in frappe.db.sql(
			"""SELECT pi.item_code, pi.rate
			FROM `tabPurchase Order Item` pi JOIN `tabPurchase Order` po ON po.name = pi.parent
			WHERE po.docstatus = 1 AND pi.item_code IN %(codes)s
			ORDER BY po.transaction_date, po.creation""",
			{"codes": tuple(codes[i : i + 2000])},
			as_dict=True,
		):
			res[r.item_code] = flt(r.rate, 4)  # later rows overwrite earlier -> latest PO wins
	return res


# --------------------------------------------------------------------------
# Stock by store (companion table for the Inventory model)
# --------------------------------------------------------------------------
@frappe.whitelist()
def stock_by_store(limit=None, offset=None, warehouse=None):
	"""Current on-hand and on-order quantity per item and warehouse (non-zero bins only)."""
	_check()
	limit, offset = _page(limit, offset)
	wh = "AND warehouse = %(wh)s" if warehouse else ""
	rows = frappe.db.sql(
		f"""SELECT item_code, warehouse, actual_qty, ordered_qty, stock_value
		FROM `tabBin` WHERE (actual_qty != 0 OR ordered_qty != 0) {wh}
		ORDER BY item_code, warehouse LIMIT %(lim)s OFFSET %(offset)s""",
		{"lim": limit + 1, "offset": offset, "wh": warehouse},
		as_dict=True,
	)
	out = [
		{
			"item_code": r.item_code,
			"Store code": r.warehouse,
			"QTY OH": flt(r.actual_qty),
			"po_qty": flt(r.ordered_qty),
			"stock_value": flt(r.stock_value, 4),
		}
		for r in rows[:limit]
	]
	return _envelope(out + rows[limit:], limit, offset)
