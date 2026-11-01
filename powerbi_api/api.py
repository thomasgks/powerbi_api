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
# reference is stored elsewhere.
SALES_INVOICE_ANIMO_FIELD = "po_no"

# Transfers are Stock Entries of type Material Receipt (transfer in), Material Issue
# (transfer out) and Material Transfer. If some Receipt/Issue entries are really stock
# adjustments, list their custom_doc_no ("Doc No") prefixes here, e.g. ("ADJ.",). Those
# entries are then reported by the adjustments endpoint instead of transfers.
ADJUSTMENT_DOC_PREFIXES = ()

# Item images are stored as relative paths (/files/xxx.jpg) on Style Attribute Variant.
# This host is prepended to give the complete URL.
IMAGE_BASE_URL = "https://ecom.ozdah.com"

DOCSTATUS_LABEL = {0: "Draft", 1: "Submitted", 2: "Cancelled"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _check():
	frappe.only_for(ROLES)


def _col(doctype, alias, field):
	"""`alias`.`field` if the column exists on this site, else NULL (custom fields vary per site)."""
	return f"{alias}.`{field}`" if frappe.db.has_column(doctype, field) else "NULL"


def _doc_no():
	return "se.custom_doc_no"  # "Doc No" on Stock Entry (Slip# Animo)


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
@frappe.whitelist()
def sales(from_date=None, to_date=None, limit=None, offset=None, docstatus=None):
	"""One row per Sales Invoice line (returns come through with negative qty/amounts)."""
	_check()
	limit, offset = _page(limit, offset)
	fd, td = _dates(from_date, to_date)

	rows = frappe.db.sql(
		f"""
		SELECT p.name AS receipt_no, p.posting_date, p.status,
			COALESCE(NULLIF(i.warehouse,''), p.set_warehouse) AS store,
			{_col("Sales Invoice", "p", SALES_INVOICE_ANIMO_FIELD)} AS animo_ref, p.customer_name, p.contact_mobile,
			p.address_display, p.total_taxes_and_charges AS doc_tax, p.net_total AS doc_net_total,
			i.idx, i.item_code, i.qty, i.net_amount, i.discount_amount, i.distributed_discount_amount,
			i.pricing_rules, i.incoming_rate AS cost
		FROM `tabSales Invoice` p
		JOIN `tabSales Invoice Item` i ON i.parent = p.name
		WHERE p.docstatus IN %(docstatus)s
			AND p.posting_date BETWEEN %(from_date)s AND %(to_date)s
		ORDER BY p.posting_date, p.name, i.idx
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
	pay = _payment_types({r.receipt_no for r in page})

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
				"Payment type": pay.get(r.receipt_no, ""),
				"Promo code": r.pricing_rules or "",
			}
		)
	return _envelope(out + rows[limit:], limit, offset)


def _payment_types(names):
	if not names:
		return {}
	rows = frappe.db.sql(
		"""
		SELECT parent, mode_of_payment FROM `tabSales Invoice Payment`
		WHERE parent IN %(names)s AND parenttype = 'Sales Invoice' AND amount != 0
		ORDER BY idx
		""",
		{"names": tuple(names)},
		as_dict=True,
	)
	res = {}
	for r in rows:
		cur = res.setdefault(r.parent, [])
		if r.mode_of_payment not in cur:
			cur.append(r.mode_of_payment)
	return {k: ", ".join(v) for k, v in res.items()}


def _adjustment_slip_sql():
	"""(SQL, params) matching Stock Entries whose Doc No has an adjustment prefix, or (None, {})."""
	params = {f"ap{i}": f"{p}%" for i, p in enumerate(ADJUSTMENT_DOC_PREFIXES)}
	if not params:
		return None, {}
	cond = " OR ".join(f"IFNULL({_doc_no()},'') LIKE %({k})s" for k in params)
	return f"({cond})", params


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
	"""One row per Stock Entry line that is a stock transfer.

	Stock Entry Type Material Issue = transfer out (from store only), Material Receipt =
	transfer in (to store only), Material Transfer = both stores. "Direction" says which
	(OUT / IN / TRANSFER) so the two legs can be paired in Power BI.

	from/to store OH = stock on hand in that warehouse right after the entry
	(from the Stock Ledger), not today's balance.
	"""
	_check()
	limit, offset = _page(limit, offset)
	fd, td = _dates(from_date, to_date)

	adj_sql, adj_params = _adjustment_slip_sql()
	not_adj = f"AND NOT {adj_sql}" if adj_sql else ""
	from_wh = "COALESCE(NULLIF(d.s_warehouse,''), se.from_warehouse)"
	to_wh = "COALESCE(NULLIF(d.t_warehouse,''), se.to_warehouse)"
	rows = frappe.db.sql(
		f"""
		SELECT se.name AS slip_erp, {_doc_no()} AS slip_animo, {_STATUS_SQL} AS status,
			se.posting_date, {from_wh} AS from_store, {to_wh} AS to_store,
			d.item_code, d.transfer_qty AS qty, d.basic_rate AS cost, d.idx,
			CASE se.stock_entry_type WHEN 'Material Issue' THEN 'OUT'
				WHEN 'Material Receipt' THEN 'IN' ELSE 'TRANSFER' END AS direction,
			{_SLE_AFTER.format(v="se", wh=from_wh)} AS from_oh,
			{_SLE_AFTER.format(v="se", wh=to_wh)} AS to_oh
		FROM `tabStock Entry` se
		JOIN `tabStock Entry Detail` d ON d.parent = se.name
		WHERE se.stock_entry_type IN ('Material Transfer','Material Receipt','Material Issue')
			{not_adj}
			AND se.docstatus IN %(docstatus)s
			AND se.posting_date BETWEEN %(from_date)s AND %(to_date)s
		ORDER BY se.posting_date, se.name, d.idx
		LIMIT %(lim)s OFFSET %(offset)s
		""",
		{
			**adj_params,
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
				"Direction": r.direction,
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
	"""Stock adjustments: Stock Reconciliations, plus Material Receipt / Material Issue entries
	whose Doc No starts with a prefix in ADJUSTMENT_DOC_PREFIXES (none by default).

	OH Qty = quantity on hand before the adjustment; adj qty = signed change.
	"""
	_check()
	limit, offset = _page(limit, offset)
	fd, td = _dates(from_date, to_date)

	adj_sql, adj_params = _adjustment_slip_sql()
	se_store = "COALESCE(NULLIF(d.t_warehouse,''), NULLIF(d.s_warehouse,''))"
	se_after = _SLE_AFTER.format(v="se", wh=se_store)
	se_adj = "IF(IFNULL(d.t_warehouse,'') != '', d.transfer_qty, -d.transfer_qty)"
	status = "CASE {t}.docstatus WHEN 0 THEN 'Draft' WHEN 1 THEN 'Submitted' ELSE 'Cancelled' END"

	se_branch = ""
	if adj_sql:  # Stock Entry adjustments only exist when ADJUSTMENT_DOC_PREFIXES is set
		se_branch = f"""
			SELECT se.name AS slip_erp, {_doc_no()} AS slip_animo, {status.format(t='se')} AS status,
				se.posting_date, {se_store} AS store, d.item_code,
				{se_after} - {se_adj} AS oh_qty, {se_adj} AS adj_qty, d.basic_rate AS cost,
				IF(se.is_opening = 'Yes', {se_adj}, NULL) AS opening_stock,
				se.stock_entry_type AS adj_type, d.idx
			FROM `tabStock Entry` se
			JOIN `tabStock Entry Detail` d ON d.parent = se.name
			WHERE se.stock_entry_type IN ('Material Receipt','Material Issue') AND {adj_sql}
				AND se.docstatus IN %(docstatus)s
				AND se.posting_date BETWEEN %(from_date)s AND %(to_date)s
			UNION ALL"""

	rows = frappe.db.sql(
		f"""
		SELECT * FROM (
			{se_branch}
			SELECT sr.name AS slip_erp, NULL AS slip_animo, {status.format(t='sr')} AS status,
				sr.posting_date, i.warehouse AS store, i.item_code, i.current_qty AS oh_qty,
				IFNULL(i.qty,0) - IFNULL(i.current_qty,0) AS adj_qty, i.valuation_rate AS cost,
				IF(sr.purpose = 'Opening Stock', i.qty, NULL) AS opening_stock,
				'Stock Reconciliation' AS adj_type, i.idx
			FROM `tabStock Reconciliation` sr
			JOIN `tabStock Reconciliation Item` i ON i.parent = sr.name
			WHERE sr.docstatus IN %(docstatus)s
				AND sr.posting_date BETWEEN %(from_date)s AND %(to_date)s
		) t
		ORDER BY posting_date, slip_erp, idx
		LIMIT %(lim)s OFFSET %(offset)s
		""",
		{
			**adj_params,
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
	images = _item_images(codes)

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
				"image": images.get(r.item_code),
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


def _item_images(codes):
	"""Complete URL of the first image of the Style Attribute Variant that lists each item."""
	res = {}
	codes = list(codes)
	for i in range(0, len(codes), 2000):
		for r in frappe.db.sql(
			"""SELECT cs.item_code, si.image
			FROM `tabColor Size Item` cs
			JOIN `tabWebsite Slideshow Item` si ON si.parent = cs.parent
				AND si.parenttype = 'Style Attribute Variant' AND si.parentfield = 'images'
			WHERE cs.parenttype = 'Style Attribute Variant' AND cs.item_code IN %(codes)s
				AND IFNULL(si.image, '') != ''
			ORDER BY si.idx DESC""",
			{"codes": tuple(codes[i : i + 2000])},
			as_dict=True,
		):
			img = r.image.strip()
			if not img.lower().startswith(("http://", "https://")):
				img = IMAGE_BASE_URL.rstrip("/") + "/" + img.lstrip("/")
			res[r.item_code] = img  # idx DESC order -> lowest idx (first image) wins
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
