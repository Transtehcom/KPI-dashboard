use super::*;
use std::fmt::Write;

#[derive(Copy, Clone, PartialEq, Eq)]
enum Kind {
    Debt,
    Receipt,
    Payment,
    PlanIn,
    PlanOut,
    ManualOut,
}

impl Kind {
    fn label(self) -> &'static str {
        match self {
            Self::Debt => "Заказ клиента",
            Self::Receipt => "Поступление безналичных ДС",
            Self::Payment => "Списание безналичных ДС",
            Self::PlanIn => "Плановое поступление ДС",
            Self::PlanOut => "Заявка на расходование ДС",
            Self::ManualOut => "Ручной план выплаты",
        }
    }
}

struct Row {
    no: String,
    kind: Kind,
    basis: String,
    party: &'static str,
    owner: &'static str,
    date: Date,
    amount: i64,
    paid: i64,
    status: &'static str,
}
impl Row {
    fn value(&self) -> i64 {
        if self.kind == Kind::Debt {
            (self.amount - self.paid).max(0)
        } else {
            self.amount
        }
    }
    fn scheduled(&self, s: &State) -> Date {
        s.values
            .get(&format!("ops:money:date:{}", self.no))
            .and_then(|d| Date::parse(d))
            .unwrap_or(self.date)
    }
    fn editable(&self) -> bool {
        matches!(
            self.kind,
            Kind::Debt | Kind::PlanIn | Kind::PlanOut | Kind::ManualOut
        )
    }
    fn outgoing(&self) -> bool {
        match self.kind {
            Kind::PlanOut => matches!(self.status, "Согласована" | "Не согласована" | "К оплате"),
            Kind::ManualOut => self.status == "Согласована",
            _ => false,
        }
    }
}
fn dataset() -> Vec<Row> {
    let mut rows = Vec::new();
    for m in 1..=11 {
        for i in 0..17_usize {
            let d = Date(2026, m, 1 + (i as u32 * 3 + m) % 27);
            if d > SNAPSHOT {
                continue;
            }
            let n = (m as usize - 1) * 17 + i;
            let buyer = BUYERS[n % BUYERS.len()];
            let manager = SELLERS[n % SELLERS.len()];
            let order = format!("ЗК-{:04}", 1600 + n);
            let booked = (78200 + ((n * 18_433) % 650_000)) as i64 * 100 + (37 * n % 100) as i64;
            let due = d.add(5 + (n % 18) as u32);
            let receipt = d.add((n % 10) as u32 + 1);
            let paid = if receipt > SNAPSHOT || n % 11 == 3 {
                0
            } else if n % 3 == 0 {
                booked * 3 / 5
            } else {
                booked
            };
            rows.push(Row {
                no: order.clone(),
                kind: Kind::Debt,
                basis: format!("Договор П-{:04}", 900 + n),
                party: buyer,
                owner: manager,
                date: due,
                amount: booked,
                paid,
                status: if paid == booked {
                    "Оплачен"
                } else {
                    "К оплате"
                },
            });
            if paid > 0 {
                rows.push(Row {
                    no: format!("ПБ-{:04}", 6110 + n),
                    kind: Kind::Receipt,
                    basis: order.clone(),
                    party: buyer,
                    owner: manager,
                    date: receipt,
                    amount: paid,
                    paid: 0,
                    status: "Проведено банком",
                });
            }
            if n % 4 != 2 {
                rows.push(Row {
                    no: format!("ПП-{:04}", 5010 + n),
                    kind: Kind::PlanIn,
                    basis: order.clone(),
                    party: buyer,
                    owner: manager,
                    date: due,
                    amount: booked,
                    paid: 0,
                    status: "Ожидаем",
                });
            }
            let vendor = VENDORS[n % VENDORS.len()];
            let request = format!("ЗР-{:04}", 4000 + n);
            let cash = booked * 67 / 100;
            let day = d.add(2 + (n % 8) as u32);
            let settled = n % 5 > 1 && day <= SNAPSHOT;
            let status = if settled {
                "Полностью оплачена"
            } else {
                ["Согласована", "Не согласована", "К оплате", "Отклонена"][i % 4]
            };
            rows.push(Row {
                no: request.clone(),
                kind: Kind::PlanOut,
                basis: format!("ЗП-{:04}", 2970 + n),
                party: vendor,
                owner: "supply",
                date: day,
                amount: cash,
                paid: 0,
                status,
            });
            if settled {
                rows.push(Row {
                    no: format!("СБ-{:04}", 7210 + n),
                    kind: Kind::Payment,
                    basis: request,
                    party: vendor,
                    owner: "supply",
                    date: day,
                    amount: cash,
                    paid: 0,
                    status: "Проведено банком",
                });
            }
            if i % 6 == 0 {
                rows.push(Row {
                    no: format!("РП-{:04}", 1000 + n),
                    kind: Kind::ManualOut,
                    basis: order,
                    party: CARRIERS[n % 3],
                    owner: "logistics",
                    date: d.add(9),
                    amount: cash / 7,
                    paid: 0,
                    status: if i == 6 {
                        "Не согласована"
                    } else {
                        "Согласована"
                    },
                });
            }
        }
    }
    rows
}

fn rub(amount: i64) -> String {
    let formatted = crate::money(amount.abs() / 100);
    let integral = formatted.trim_end_matches(" ₽");
    format!(
        "{}{integral},{:02} ₽",
        if amount < 0 { "−" } else { "" },
        amount.unsigned_abs() % 100
    )
}
fn basis_filter(s: &State, r: &Row) -> bool {
    allowed(s, r.owner)
}
fn category(s: &State, r: &Row, key: &str) -> bool {
    if !basis_filter(s, r) {
        return false;
    }
    let period = range(s);
    match key {
        "debt" => r.kind == Kind::Debt && r.value() > 0,
        "receipts" => r.kind == Kind::Receipt && in_range(r.date, period),
        "paid" => r.kind == Kind::Payment && in_range(r.date, period),
        "plans" => r.outgoing() && in_range(r.scheduled(s), period),
        _ => false,
    }
}
const CARDS: [(&str, &str); 4] = [
    ("debt", "Дебиторская задолженность"),
    ("receipts", "Поступления периода"),
    ("paid", "Выплаты периода"),
    ("plans", "Плановые выплаты"),
];
fn sums(s: &State, data: &[Row]) -> [i64; 4] {
    CARDS.map(|(key, _)| {
        data.iter()
            .filter(|r| category(s, r, key))
            .map(Row::value)
            .sum()
    })
}
pub(super) fn totals(s: &State) -> [i64; 4] {
    sums(s, &dataset()).map(|c| (c + 50) / 100)
}

fn opening(s: &State, data: &[Row]) -> i64 {
    let first = range(s).0;
    let seed = if s.role == "admin" && (s.person == "all" || s.person.is_empty()) {
        3_291_040_00
    } else {
        0
    };
    let actual: i64 = data
        .iter()
        .filter(|r| basis_filter(s, r) && r.date < first)
        .map(|r| match r.kind {
            Kind::Receipt => r.amount,
            Kind::Payment => -r.amount,
            _ => 0,
        })
        .sum();
    seed + actual
}
fn day_contains(s: &State, r: &Row, key: &str, d: Date) -> bool {
    basis_filter(s, r)
        && r.scheduled(s) == d
        && match key {
            "day-in" => r.kind == Kind::PlanIn,
            "day-out" => r.outgoing(),
            _ => false,
        }
}

fn register(s: &State, rows: &[&Row], title: &str, id: &str) -> String {
    let mut html = format!(
        r#"<section class="panel" id="{id}"><div class="panel-head"><h2>{}</h2><span class="chip">{}</span></div><div class="table-scroll"><table><thead><tr><th>Документ</th><th>Контрагент</th><th>Дата</th><th>Остаток / сумма</th><th>Состояние</th></tr></thead><tbody>"#,
        esc(title),
        rows.len()
    );
    if rows.is_empty() {
        html += &empty(5);
    }
    for r in rows {
        let day = r.scheduled(s);
        write!(
            html,
            r#"<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>"#,
            button("ops-doc", &r.no, &r.no, s.selected == r.no),
            esc(r.party),
            day.show(),
            rub(r.value()),
            esc(r.status)
        )
        .unwrap();
        if s.selected == r.no {
            write!(html,r#"<tr><td colspan="5"><div class="detail" id="ops-record"><strong>{} · {}</strong><p>{} · Ответственный {}</p><p>Основание {} · Сумма документа {}</p>"#,r.kind.label(),esc(&r.no),esc(r.party),esc(name(r.owner)),esc(&r.basis),rub(r.amount)).unwrap();
            if r.kind == Kind::Debt {
                write!(
                    html,
                    "<p>Оплачено {} · Остаток {} · Договорная дата {}</p>",
                    rub(r.paid),
                    rub(r.value()),
                    r.date.show()
                )
                .unwrap();
            }
            if s.role == "admin" && r.editable() {
                write!(
                    html,
                    r#"<label>{} <input type="date" data-act="ops-money-date" value="{}"></label>"#,
                    if r.kind == Kind::Debt {
                        "Ожидаем оплату"
                    } else {
                        "Плановая дата"
                    },
                    day.iso()
                )
                .unwrap();
                if day != r.date {
                    html += &button("ops-money-date", "", "Вернуть исходную дату", false);
                }
            }
            html += "</div></td></tr>";
        }
    }
    html += "</tbody></table></div></section>";
    html
}
pub(super) fn set_date(s: &mut State, v: &str) -> bool {
    if s.route != "money" || s.role != "admin" {
        return false;
    }
    let Some(row) = dataset()
        .into_iter()
        .find(|r| r.no == s.selected && r.editable() && basis_filter(s, r))
    else {
        return false;
    };
    let key = format!("ops:money:date:{}", row.no);
    if v.is_empty() {
        s.values.remove(&key);
        return true;
    }
    let Some(date) = Date::parse(v) else {
        return false;
    };
    s.values.insert(key, date.iso());
    true
}
pub(super) fn render(s: &State) -> String {
    let data = dataset();
    let active = drill(s);
    let ((first, last), amounts) = (range(s), sums(s, &data));
    let mut html = title("Финансы · 1С", "Деньги");
    html += r#"<div class="stat-grid">"#;
    for ((key, label), amount) in CARDS.into_iter().zip(amounts) {
        html += &tile(key, label, &rub(amount), key == active);
    }
    html += "</div>";
    let start = opening(s, &data);
    write!(html,r#"<section class="panel"><div class="panel-head"><h2>Остаток на начало периода</h2><strong>{}</strong></div></section>"#,rub(start)).unwrap();
    let mut incoming = Vec::new();
    let mut outgoing = Vec::new();
    let mut closing = Vec::new();
    let mut cash = start;
    let mut minimum = (cash, first);
    for day in 1..=last.2 {
        let date = Date(first.0, first.1, day);
        let sum = |key: &str| {
            data.iter()
                .filter(|r| day_contains(s, r, key, date))
                .map(Row::value)
                .sum::<i64>()
        };
        let (i, o) = (sum("day-in"), sum("day-out"));
        incoming.push(i);
        outgoing.push(o);
        cash += i - o;
        closing.push(cash);
        if day == 1 || cash < minimum.0 {
            minimum = (cash, date);
        }
    }
    html += r#"<section class="panel"><div class="panel-head"><h2>Платёжный календарь</h2></div><div class="table-scroll"><table><thead><tr><th>Дата</th>"#;
    for d in 1..=last.2 {
        write!(html, "<th>{:02}.{:02}</th>", d, first.1).unwrap();
    }
    html += "</tr></thead><tbody>";
    for (label, key, values) in [
        ("Поступления", "day-in", incoming),
        ("Выплаты", "day-out", outgoing),
        ("Остаток на конец дня", "day-balance", closing),
    ] {
        write!(html, "<tr><th>{label}</th>").unwrap();
        for (i, cents) in values.into_iter().enumerate() {
            let id = format!("{key}:{}", Date(first.0, first.1, 1 + i as u32).iso());
            if cents == 0 && key != "day-balance" {
                html += "<td>—</td>";
            } else {
                write!(
                    html,
                    r#"<td>{}</td>"#,
                    button("ops-drill", &id, &rub(cents), id == active)
                )
                .unwrap();
            }
        }
        html += "</tr>";
    }
    write!(html,r#"</tbody></table></div><p class="muted">Максимальная нехватка {} · Дата минимума {}</p></section>"#,rub((-minimum.0).max(0)),minimum.1.show()).unwrap();
    let mut selected: Vec<&Row> = Vec::new();
    let heading = if let Some((_, label)) = CARDS.iter().find(|(key, _)| *key == active) {
        selected = data.iter().filter(|r| category(s, r, active)).collect();
        label.to_string()
    } else if let Some((category, day)) = active.split_once(':').and_then(|(k, v)| {
        Date::parse(v)
            .filter(|d| in_range(*d, (first, last)))
            .map(|d| (k, d))
    }) {
        if category == "day-balance" {
            let plan = data
                .iter()
                .filter(|r| {
                    basis_filter(s, r)
                        && (r.kind == Kind::PlanIn || r.outgoing())
                        && in_range(r.scheduled(s), (first, day))
                })
                .collect::<Vec<_>>();
            let i: i64 = plan
                .iter()
                .filter(|r| r.kind == Kind::PlanIn)
                .map(|r| r.amount)
                .sum();
            let o: i64 = plan.iter().filter(|r| r.outgoing()).map(|r| r.amount).sum();
            write!(html,r#"<div class="panel detail">На начало месяца {} + поступления {} − выплаты {} = <strong>{}</strong></div>"#,rub(start),rub(i),rub(o),rub(start+i-o)).unwrap();
            selected = plan;
            format!("Движения по {}", day.show())
        } else {
            selected = data
                .iter()
                .filter(|r| day_contains(s, r, category, day))
                .collect();
            format!(
                "{} · {}",
                if category == "day-in" {
                    "План поступлений"
                } else {
                    "План выплат"
                },
                day.show()
            )
        }
    } else {
        String::new()
    };
    if !heading.is_empty() {
        selected.sort_by_key(|r| (r.scheduled(s), r.no.as_str()));
        html += &register(s, &selected, &heading, "ops-detail");
    }
    if active != "debt" {
        let mut debt = data
            .iter()
            .filter(|r| category(s, r, "debt"))
            .collect::<Vec<_>>();
        debt.sort_by_key(|r| (r.scheduled(s), r.no.as_str()));
        html += &register(
            s,
            &debt,
            "Кто должен и когда ожидаем поступления",
            "ops-debtors",
        );
    }
    html
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn totals_and_calendar_use_same_approved_plan_and_real_payments() {
        let s = State::default();
        let data = dataset();
        let (_, last) = range(&s);
        let days: i64 = (1..=last.2)
            .flat_map(|d| {
                data.iter()
                    .filter(|r| day_contains(&s, r, "day-out", Date(last.0, last.1, d)))
                    .map(Row::value)
                    .collect::<Vec<_>>()
            })
            .sum();
        assert!(days > 0);
        assert_eq!(days, sums(&s, &data)[3]);
        assert!(data
            .iter()
            .filter(|r| category(&s, r, "debt"))
            .all(|r| r.paid < r.amount));
        assert!(data
            .iter()
            .filter(|r| category(&s, r, "paid"))
            .all(|r| r.kind == Kind::Payment));
    }
    #[test]
    fn promise_changes_neither_contract_nor_calendar() {
        let mut s = State {
            route: "money".into(),
            ..Default::default()
        };
        let data = dataset();
        let r = data.iter().find(|r| category(&s, r, "debt")).unwrap();
        let before = sums(&s, &data);
        s.selected = r.no.clone();
        assert!(set_date(&mut s, "2026-12-19"));
        assert_ne!(r.date, r.scheduled(&s));
        assert_eq!(before, sums(&s, &data));
        s.role = "employee".into();
        assert!(!set_date(&mut s, "2026-12-20"));
    }
}
