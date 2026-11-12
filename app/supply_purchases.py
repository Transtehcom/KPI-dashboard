"""Purchases above the KPI threshold, with explicit lot grouping."""


def qualifying_purchase_groups(rows, threshold):
    grouped = {}
    for source in rows:
        row = dict(source)
        lot = (row["lot"] or "").strip()
        key = ("lot", lot.casefold()) if lot else ("order", row["id"])
        grouped.setdefault(key, []).append(row)
    result = []
    for key, items in grouped.items():
        amount = sum(row["amount_novat"] or 0 for row in items)
        if amount >= threshold:
            result.append((key, items, amount))
    return result
