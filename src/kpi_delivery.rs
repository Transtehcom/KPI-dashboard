use super::{doc, group, month_days, month_parts, row, Group, Measure, Row};
use chrono::{Datelike, NaiveDate};
use std::collections::BTreeMap;

#[derive(Clone)]
struct CustomerOrder {
    id: Option<usize>,
    procurement: bool,
    plan: Option<NaiveDate>,
    actual: Option<NaiveDate>,
    net_plan: Option<i64>,
    net_actual: Option<i64>,
    gross_actual: Option<i64>,
    // Percentage with three decimal places, so 99.999% remains incomplete.
    shipment: Option<i32>,
}

struct Link {
    date: NaiveDate,
    document: String,
    order: CustomerOrder,
}

fn date(y: i32, m: u32, d: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, m, d).expect("validated simulation date")
}
fn asof(y: i32, m: u32) -> NaiveDate {
    date(y, m, month_days(y, m)).min(date(2026, 11, 23))
}
fn label(v: Option<NaiveDate>) -> String {
    v.map(|t| t.format("%d.%m.%Y").to_string())
        .unwrap_or_else(|| "—".into())
}
fn rubles(v: Option<i64>) -> String {
    v.map(|kop| {
        format!(
            "{}{:02} ₽",
            crate::money(kop / 100).trim_end_matches(" ₽").to_owned() + ",",
            kop.unsigned_abs() % 100
        )
    })
    .unwrap_or_else(|| "—".into())
}
fn shipment_label(v: Option<i32>) -> String {
    v.map(|p| {
        format!("{:.3}", f64::from(p) / 1000.)
            .trim_end_matches('0')
            .trim_end_matches('.')
            .replace('.', ",")
            + "%"
    })
    .unwrap_or_else(|| "—".into())
}

fn without_vat(gross: i64) -> Option<i64> {
    let scaled = i128::from(gross) * 100;
    let net = (scaled.abs() + 61) / 122 * scaled.signum();
    i64::try_from(net).ok()
}

fn budget(order: &CustomerOrder) -> (Option<bool>, Option<i64>) {
    let net = order
        .net_actual
        .or_else(|| order.gross_actual.and_then(without_vat));
    let auto = match order.shipment {
        Some(0..=99_999) => Some(false),
        Some(100_000) => order
            .net_plan
            .zip(net)
            .map(|(expected, actual)| actual <= expected),
        _ => None,
    };
    (auto, net)
}

fn delivery(order: &CustomerOrder, asof: NaiveDate) -> (Option<bool>, Option<i64>) {
    let deviation = order.plan.and_then(|plan| {
        let end = order.actual.or_else(|| (plan < asof).then_some(asof))?;
        Some((plan - end).num_days())
    });
    (
        order.plan.map(|_| deviation.is_none_or(|d| d >= 0)),
        deviation,
    )
}

fn logistics_group(
    period: &str,
    asof: NaiveDate,
    mut links: Vec<Link>,
    budget_mode: bool,
) -> Group {
    let (y, m) = month_parts(period);
    links.retain(|l| {
        l.date.year() == y && l.date.month() == m && (!budget_mode || l.order.procurement)
    });
    links.sort_by(|a, b| (a.date, &a.document).cmp(&(b.date, &b.document)));
    let mut customers: BTreeMap<usize, Vec<Link>> = BTreeMap::new();
    for link in links {
        if let Some(id) = link.order.id {
            customers.entry(id).or_default().push(link);
        }
    }
    let mut g = if budget_mode {
        group(
            "Полностью отгруженные заказы в пределах бюджета",
            "1С",
            Measure::Share(100),
        )
    } else {
        group("Поставки в срок", "1С", Measure::Share(98))
    };
    for (id, links) in customers {
        let order = &links.last().unwrap().order;
        let sources = links
            .iter()
            .map(|l| l.document.as_str())
            .collect::<Vec<_>>()
            .join(", ");
        let (auto, detail) = if budget_mode {
            let (result, net) = budget(order);
            (
                result,
                format!(
                    "В закуп · отгрузка {} · доставка без НДС: ожидаемая {}, фактическая {} · {}",
                    shipment_label(order.shipment),
                    rubles(order.net_plan),
                    rubles(net),
                    sources
                ),
            )
        } else {
            let (result, delta) = delivery(order, asof);
            let status = match (result, order.actual) {
                (None, _) => "Не задана конечная дата поставки",
                (Some(false), _) => "Просрочен",
                (Some(true), None) => "Ожидает поставки, срок не прошёл",
                _ => "Без просрочки",
            };
            (
                result,
                format!(
                    "Конечная дата поставки {} · факт {} · {} · {} · {}",
                    label(order.plan),
                    label(order.actual),
                    delta
                        .map(|d| format!("{d:+} дн."))
                        .unwrap_or_else(|| "—".into()),
                    status,
                    sources
                ),
            )
        };
        let mut r = row(
            doc("Заказ клиента", period, id),
            detail,
            auto.unwrap_or(false),
            1,
        );
        r.unknown = auto.is_none();
        g.rows.push(r);
    }
    g
}

pub(super) fn logistics(period: &str, budget_mode: bool) -> Group {
    let (y, m) = month_parts(period);
    let mut links = Vec::new();
    for i in 0..10 {
        let expected = (42_000 + 9_500 * (i as i64 + 1)) * 100 + 33;
        let net = expected
            + if (i + m as usize) % 5 == 0 {
                730_010
            } else {
                -480_030
            };
        let planned = 3 + i as u32 * 2;
        let order = CustomerOrder {
            id: Some(i),
            procurement: i != 8,
            plan: Some(date(y, m, if i == 7 { 25 } else { planned })),
            actual: (i != 7 && i != 6).then(|| {
                date(
                    y,
                    m,
                    if (i + m as usize) % 5 == 0 {
                        planned + 1
                    } else {
                        planned
                    },
                )
            }),
            net_plan: Some(expected),
            net_actual: (i % 3 != 0).then_some(net),
            gross_actual: Some((net * 122 + 50) / 100),
            shipment: Some(if i == 3 { 89_000 } else { 100_000 }),
        };
        links.push(Link {
            date: date(y, m, 2 + i as u32),
            document: doc("Поручение", period, i),
            order: order.clone(),
        });
        if i % 3 == 0 {
            links.push(Link {
                date: date(y, m, 1 + i as u32),
                document: doc("Заявка ТР", period, i),
                order,
            });
        }
    }
    logistics_group(period, asof(y, m), links, budget_mode)
}

#[derive(Clone)]
struct SupplierOrder {
    customer: usize,
    document: String,
    plan: Option<NaiveDate>,
    actual: Option<NaiveDate>,
    complete: bool,
    defect: bool,
    repaired: Option<NaiveDate>,
}

fn supply_group(
    period: &str,
    today: NaiveDate,
    asof: NaiveDate,
    orders: Vec<SupplierOrder>,
) -> Group {
    let (y, m) = month_parts(period);
    let mut customers: BTreeMap<usize, Vec<SupplierOrder>> = BTreeMap::new();
    for order in orders {
        customers.entry(order.customer).or_default().push(order);
    }
    let mut g = group(
        "Поставки в полном объёме без нарушений",
        "1С",
        Measure::LateShare(20),
    );
    let cutoff = asof.min(date(y, m, month_days(y, m)));
    for (id, items) in customers {
        let Some(planned) = items.iter().filter_map(|r| r.plan).min() else {
            continue;
        };
        if planned.year() != y || planned.month() != m {
            continue;
        }
        let actual = items.iter().filter_map(|r| r.actual).max();
        let pending = planned >= today && actual.is_none_or(|f| f > cutoff);
        let mut reasons = Vec::new();
        match actual {
            None => reasons.push("Не поставлено на дату расчёта".into()),
            Some(f) if f > asof => reasons.push("Не поставлено на дату расчёта".into()),
            Some(f) if f > planned => {
                reasons.push(format!("Опоздание {} дн.", (f - planned).num_days()))
            }
            _ => {}
        }
        if actual.is_some() && items.iter().any(|r| !r.complete) {
            reasons.push("Недопоставка".into());
        }
        for r in &items {
            if !r.defect {
                continue;
            }
            if r.repaired.is_none_or(|d| d > asof) {
                reasons.push(format!("{}: брак ещё не устранён", r.document));
            } else if r.repaired.is_some_and(|d| d > planned) {
                reasons.push(format!("{}: брак исправлен позже срока", r.document));
            }
        }
        let ok = reasons.is_empty() && !pending;
        let status = if pending {
            "Ожидается в пределах срока".into()
        } else if ok {
            "В срок и в полном объёме".into()
        } else {
            reasons.join("; ")
        };
        let docs = items
            .iter()
            .map(|r| r.document.as_str())
            .collect::<Vec<_>>()
            .join(", ");
        let mut r: Row = row(
            doc("Заказ клиента", period, id),
            format!(
                "Конечная дата {} · факт {} · {status} · {docs}",
                label(Some(planned)),
                label(actual)
            ),
            ok,
            1,
        );
        r.pending = pending;
        g.rows.push(r);
    }
    g
}

pub(super) fn supply(period: &str) -> Group {
    let (y, m) = month_parts(period);
    let mut orders = Vec::new();
    for i in 0..9 {
        let d = if i == 8 { 26 } else { 3 + i as u32 * 2 };
        let order = SupplierOrder {
            customer: i,
            document: doc("Заказ поставщику", period, i),
            plan: Some(date(y, m, d)),
            actual: (i != 8).then(|| date(y, m, if (m as usize + i) % 6 == 0 { d + 1 } else { d })),
            complete: i != 2,
            defect: i == 3,
            repaired: None,
        };
        if i % 3 == 0 {
            let mut extra = order.clone();
            extra.document = doc("Заказ поставщику", period, 20 + i);
            orders.push(extra);
        }
        orders.push(order);
    }
    supply_group(period, date(2026, 11, 23), asof(y, m), orders)
}

#[cfg(test)]
mod parity {
    use super::super::{rules_for, scored};
    use super::*;
    use crate::State;
    const PERIOD: &str = "2026-11";
    fn nov(d: u32) -> NaiveDate {
        date(2026, 11, d)
    }
    fn order(id: usize) -> CustomerOrder {
        CustomerOrder {
            id: Some(id),
            procurement: true,
            plan: Some(nov(10)),
            actual: Some(nov(10)),
            net_plan: Some(10_000),
            net_actual: None,
            gross_actual: Some(12_200),
            shipment: Some(100_000),
        }
    }
    fn link(o: CustomerOrder) -> Link {
        Link {
            order: o,
            date: nov(5),
            document: "Поручение Д-001".into(),
        }
    }
    fn pay(code: &str, group: Group) -> (Option<i64>, i64) {
        let person = if code == "on_time_delivery" {
            "supply"
        } else {
            "logistics"
        };
        let rule = rules_for(person, PERIOD).find(|r| r.code == code).unwrap();
        scored(&State::default(), person, rule, &[group])
    }
    // Same boundaries and expected payouts as the in-memory Python reference
    // tests in test_logistics_delivery.py, test_logistics_budget.py and test_supply_delivery.py.
    #[test]
    fn logistics_98_percent_full_and_49_percent_half_unique_linked_orders() {
        for (ok, payout) in [(98, 2500), (49, 1250), (100, 2500), (0, 0)] {
            let mut links = Vec::new();
            for i in 0..100 {
                let mut o = order(i);
                if i >= ok {
                    o.actual = Some(nov(11));
                }
                links.push(link(o));
            }
            links.push(link(links[0].order.clone()));
            let g = logistics_group(PERIOD, nov(12), links, false);
            assert_eq!(g.rows.len(), 100);
            assert_eq!(pay("logistics_delivery", g).1, payout);
        }
    }
    #[test]
    fn logistics_unknown_grace_and_unsupplied_past_due() {
        let mut o = order(0);
        o.actual = None;
        for (today, expected) in [
            (9, (Some(true), None)),
            (10, (Some(true), None)),
            (11, (Some(false), Some(-1))),
        ] {
            assert_eq!(delivery(&o, nov(today)), expected);
        }
        o.plan = None;
        assert_eq!(
            pay(
                "logistics_delivery",
                logistics_group(
                    PERIOD,
                    nov(12),
                    vec![link(order(0)), link(CustomerOrder { id: Some(1), ..o })],
                    false
                )
            ),
            (None, 0)
        );
    }
    #[test]
    fn budget_net_rounding_missing_and_partial_results_match_cash_payout() {
        let mut exceed = order(1);
        exceed.gross_actual = Some(12_201);
        assert_eq!(budget(&exceed), (Some(false), Some(10_001)));
        assert_eq!(
            pay(
                "logistics_budget",
                logistics_group(
                    PERIOD,
                    nov(12),
                    vec![link(order(0)), link(exceed.clone())],
                    true
                )
            ),
            (Some(50), 1500)
        );
        exceed.gross_actual = None;
        assert_eq!(
            pay(
                "logistics_budget",
                logistics_group(
                    PERIOD,
                    nov(12),
                    vec![link(order(0)), link(exceed.clone())],
                    true
                )
            ),
            (None, 0)
        );
        exceed.shipment = Some(99_999);
        assert_eq!(budget(&exceed), (Some(false), None));
        exceed.net_plan = Some(0);
        exceed.net_actual = Some(0);
        exceed.shipment = Some(100_000);
        assert_eq!(budget(&exceed), (Some(true), Some(0)));
        exceed.shipment = None;
        assert_eq!(budget(&exceed), (None, Some(0)));
        let empty = logistics_group(
            PERIOD,
            nov(12),
            vec![link(CustomerOrder {
                procurement: false,
                ..exceed
            })],
            true,
        );
        assert!(empty.rows.is_empty());
    }
    fn supplied(id: usize) -> SupplierOrder {
        SupplierOrder {
            customer: id,
            document: format!("Д-{id}"),
            plan: Some(nov(1)),
            actual: Some(nov(1)),
            complete: true,
            defect: false,
            repaired: None,
        }
    }
    #[test]
    fn supplier_payout_breakpoints_including_future_unfinished_and_defects() {
        for (late, payout) in [(5, 0), (2, 2250), (1, 3000), (0, 3000)] {
            let rows = (0..5)
                .map(|i| SupplierOrder {
                    actual: Some(nov(if i < late { 2 } else { 1 })),
                    ..supplied(i)
                })
                .collect();
            assert_eq!(
                pay(
                    "on_time_delivery",
                    supply_group(PERIOD, nov(3), nov(3), rows)
                )
                .1,
                payout
            );
        }
        let rows = [1, 2, 9, 11, 19]
            .into_iter()
            .enumerate()
            .map(|(i, d)| SupplierOrder {
                plan: Some(nov(d)),
                actual: None,
                ..supplied(i)
            })
            .collect();
        let g = supply_group(PERIOD, nov(5), nov(3), rows);
        assert_eq!(g.rows.iter().filter(|r| r.pending).count(), 3);
        assert_eq!(pay("on_time_delivery", g), (Some(75), 2250));
        let pair = vec![
            supplied(1),
            SupplierOrder {
                defect: true,
                repaired: Some(nov(2)),
                ..supplied(1)
            },
        ];
        let g = supply_group(PERIOD, nov(3), nov(3), pair);
        assert_eq!(g.rows.len(), 1);
        assert_eq!(pay("on_time_delivery", g), (Some(0), 0));
    }
}
