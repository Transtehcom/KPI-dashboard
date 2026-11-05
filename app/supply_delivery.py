"""Общая группировка поставок по заказу клиента для KPI и его таблицы."""
import datetime as dt


def delivery_deviation(group, asof):
    """План минус факт, для ещё не поставленного просроченного заказа — минус дата среза."""
    planned, actual = group["planned_date"], group["customer_actual_date"]
    if not planned:
        return None
    if actual:
        return (dt.date.fromisoformat(planned) - dt.date.fromisoformat(actual)).days
    if planned < asof.isoformat():
        return (dt.date.fromisoformat(planned) - asof).days
    return None


def delivery_key(r):
    """Стабильный ключ поставки, общий для выгрузки и ручного ввода."""
    return r.get("customer_order_key") or (
        f"number:{r['customer_order']}:{r['customer_order_date'][:4]}"
        if r.get("customer_order") and r.get("customer_order_date") else f"supplier:{r['id']}")


def delivery_overrides(con):
    return {r["ref"].removeprefix("delivery:"): r["value"] for r in con.execute(
        "SELECT ref,value FROM manual_fields WHERE tbl='supplier_orders' AND field='customer_actual_date' "
        "AND ref LIKE 'delivery:%'")}


def delivery_groups(rows, overrides=None):
    overrides = overrides or {}
    groups = {}
    for source in rows:
        r = dict(source)
        key = delivery_key(r)
        groups.setdefault(key, []).append(r)
    out = []
    for key, children in groups.items():
        def dates(field):
            return sorted({r[field] for r in children if r.get(field)})
        planned, actual = dates("planned_date"), dates("customer_actual_date")
        defects = [r for r in children if r.get("defect")]
        out.append({"ref": key, "doc": children[0].get("customer_order") or "Без заказа клиента",
                    "dt": children[0].get("customer_order_date"), "rows": children,
                    "planned_date": planned[0] if planned else None,
                    "source_actual_date": actual[-1] if actual else None,
                    "customer_actual_date": overrides.get(key, actual[-1] if actual else None),
                    "manual_actual": key in overrides,
                    "amount_novat": sum(r.get("amount_novat") or 0 for r in children),
                    "complete": all(r.get("complete") for r in children),
                    "defect": any(r.get("defect") for r in children),
                    "defect_fixed_date": max(r["defect_fixed_date"] for r in defects)
                    if defects and all(r.get("defect_fixed_date") for r in defects) else None})
    return out
