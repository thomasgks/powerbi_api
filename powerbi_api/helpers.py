import frappe
from frappe.utils import flt

CHUNK = 2000
PRICE_LISTS = {
	"PriceLevel1": "price_level_1",
	"PriceLevel2": "price_level_2",
	"PriceLevel3": "price_level_3",
	"Standard Selling": "original_price",
}


ITEM_CUSTOM_FIELDS = (
	"custom_style_code",
	"custom_item_name_ar",
	"custom_material",
	"custom_image_url",
	"custom_bin_no",
	"custom_dcs",
	"custom_vendor_code",
	"custom_vendor_name",
)


def _custom_cols():
	"""Item custom fields to select; any that don't exist on this site come back as NULL."""
	return ", ".join(
		f"`{f}`" if frappe.db.has_column("Item", f) else f"NULL AS `{f}`" for f in ITEM_CUSTOM_FIELDS
	)


def _chunks(seq, n=CHUNK):
	seq = list(seq)
	for i in range(0, len(seq), n):
		yield tuple(seq[i : i + n])


def split_group(item_group):
	"""'ACCESSORY.BALL.FOOTBALL' -> ('ACCESSORY', 'BALL', 'FOOTBALL')."""
	parts = (item_group or "").split(".")
	parts += [""] * (3 - len(parts))
	return parts[0], parts[1], parts[2]


def get_item_map(item_codes):
	"""Item master data for a set of item codes, in a handful of queries.

	Returns {item_code: {...item fields..., attrs: {...}, barcode, prices: {...}}}.
	"""
	codes = [c for c in item_codes if c]
	res = {}
	for chunk in _chunks(codes):
		for r in frappe.db.sql(
			f"""SELECT name, item_name, brand, item_group, variant_of, description, valuation_rate,
				is_stock_item, {_custom_cols()}
			FROM `tabItem` WHERE name IN %(c)s""",
			{"c": chunk},
			as_dict=True,
		):
			r["attrs"], r["barcode"], r["prices"] = {}, None, {}
			res[r.pop("name")] = r

		for r in frappe.db.sql(
			"""SELECT parent, attribute, attribute_value FROM `tabItem Variant Attribute`
			WHERE parenttype = 'Item' AND parent IN %(c)s""",
			{"c": chunk},
			as_dict=True,
		):
			if r.parent in res:
				res[r.parent]["attrs"][r.attribute] = r.attribute_value

		for r in frappe.db.sql(
			"""SELECT parent, barcode FROM `tabItem Barcode`
			WHERE parenttype = 'Item' AND parent IN %(c)s ORDER BY idx DESC""",
			{"c": chunk},
			as_dict=True,
		):
			if r.parent in res:
				res[r.parent]["barcode"] = r.barcode  # DESC order -> lowest idx wins

		# latest currently-valid price per list (later valid_from overwrites earlier)
		for r in frappe.db.sql(
			"""SELECT item_code, price_list, price_list_rate FROM `tabItem Price`
			WHERE item_code IN %(c)s AND price_list IN %(pl)s
				AND (valid_from IS NULL OR valid_from <= CURDATE())
				AND (valid_upto IS NULL OR valid_upto >= CURDATE())
			ORDER BY valid_from, creation""",
			{"c": chunk, "pl": tuple(PRICE_LISTS)},
			as_dict=True,
		):
			if r.item_code in res:
				res[r.item_code]["prices"][PRICE_LISTS[r.price_list]] = flt(r.price_list_rate, 4)
	return res


def item_block(m):
	"""Flatten an item map entry into the columns shared by all four models."""
	a = m.get("attrs", {})
	p = m.get("prices", {})
	dept, sub, _ = split_group(m.get("item_group"))
	return {
		"vc": m.get("custom_vendor_code"),
		"vendor_name": m.get("custom_vendor_name"),
		"brand": m.get("brand"),
		"dcs": m.get("custom_dcs"),
		"dept": dept,
		"sub_group": sub,
		"barcode": m.get("barcode"),
		"style": m.get("custom_style_code"),
		"item_name": m.get("item_name"),
		"size": a.get("Size"),
		"season": a.get("Season"),
		"assortment": a.get("Assortment"),
		"color": a.get("Color") or a.get("Colour"),
		"color_code": a.get("Color Code"),
		# Supplier: no Item Supplier rows exist, so fall back to the vendor name on the item
		"supplier": m.get("custom_vendor_name"),
		"original_price": p.get("original_price"),
		"price_level_1": p.get("price_level_1"),
		"price_level_2": p.get("price_level_2"),
		"price_level_3": p.get("price_level_3"),
	}