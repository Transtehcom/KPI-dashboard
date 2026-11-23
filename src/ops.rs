//! Report navigation and date helpers shared by sales, procurement, logistics and finance.
use crate::{employees, esc, viewer, State};

#[path = "ops_money.rs"]
mod finance;
#[path = "ops_funnel.rs"]
mod funnel;
#[path = "ops_metrics.rs"]
mod metrics;

const SNAPSHOT: Date = Date(2026, 11, 23);
const BUYERS: [&str; 6] = [
    "НПО «Полярный контур»",
    "Завод «Янтарная дуга»",
    "Бюро «Секция»",
    "Группа «Меридиан-М»",
    "КБ «Лазурная линия»",
    "Инжиниринг «Створ»",
];
const VENDORS: [&str; 4] = [
    "МК «Формовка»",
    "ТД «Металлография»",
    "ГК «Штрих»",
    "ООО «Контур стали»",
];
const CARRIERS: [&str; 3] = ["ТК «Восточная миля»", "ТК «Рейс 54»", "ТК «Северная нить»"];
const PRODUCTS: [&str; 5] = [
    "Опора У-110",
    "Мачта освещения МО-24",
    "Траверса ТМ-6",
    "Кронштейн КР-8",
    "Шкаф управления ШУ-12",
];
const SELLERS: [&str; 3] = ["sales", "tender", "tender_lead"];

#[derive(Copy, Clone, PartialEq, Eq, PartialOrd, Ord, Debug)]
struct Date(i32, u32, u32);

impl Date {
    fn month_days(year: i32, month: u32) -> u32 {
        match month {
            4 | 6 | 9 | 11 => 30,
            2 if year % 4 == 0 && (year % 100 != 0 || year % 400 == 0) => 29,
            2 => 28,
            _ => 31,
        }
    }
    fn parse(v: &str) -> Option<Self> {
        if v.len() != 10 || !v.is_ascii() || v.as_bytes()[4] != b'-' || v.as_bytes()[7] != b'-' {
            return None;
        }
        let (y, m, d) = (
            v[..4].parse().ok()?,
            v[5..7].parse().ok()?,
            v[8..].parse().ok()?,
        );
        (y >= 2000
            && y <= 2100
            && (1..=12).contains(&m)
            && (1..=Self::month_days(y, m)).contains(&d))
        .then_some(Self(y, m, d))
    }
    fn iso(self) -> String {
        format!("{:04}-{:02}-{:02}", self.0, self.1, self.2)
    }
    fn show(self) -> String {
        format!("{:02}.{:02}.{}", self.2, self.1, self.0)
    }
    fn add(mut self, days: u32) -> Self {
        self.2 += days;
        while self.2 > Self::month_days(self.0, self.1) {
            self.2 -= Self::month_days(self.0, self.1);
            self.1 += 1;
            if self.1 > 12 {
                self.0 += 1;
                self.1 = 1;
            }
        }
        self
    }
}

fn range(s: &State) -> (Date, Date) {
    let start = Date::parse(&format!("{}-01", s.period)).unwrap_or(Date(SNAPSHOT.0, SNAPSHOT.1, 1));
    let end = Date(start.0, start.1, Date::month_days(start.0, start.1));
    (start, end)
}
fn in_range(date: Date, r: (Date, Date)) -> bool {
    date >= r.0 && date <= r.1
}
fn allowed(s: &State, owner: &str) -> bool {
    (s.role == "admin" || viewer(s) == owner)
        && (s.person == "all" || s.person.is_empty() || s.person == owner)
}
fn name(id: &str) -> &'static str {
    employees()
        .into_iter()
        .find(|e| e.0 == id)
        .map(|e| e.1)
        .unwrap_or("Сотрудник")
}
fn value<'a>(s: &'a State, key: &str, default: &'a str) -> &'a str {
    s.values.get(key).map(String::as_str).unwrap_or(default)
}
fn button(key: &str, val: &str, text: &str, on: bool) -> String {
    let scroll = match key {
        "ops-stage" | "ops-drill" if !on => r#" data-scroll="ops-detail""#,
        "ops-doc" if !on => r#" data-scroll="ops-record""#,
        _ => "",
    };
    format!(
        r#"<button type="button" class="btn{}" data-act="{}" data-val="{}" aria-pressed="{}"{scroll}>{}</button>"#,
        if on { " primary" } else { "" },
        esc(key),
        esc(val),
        on,
        esc(text)
    )
}
fn people_select(s: &State, ids: &[&str]) -> String {
    if s.role != "admin" {
        return String::new();
    }
    let options = ids
        .iter()
        .map(|id| {
            format!(
                r#"<option value="{}" {}>{}</option>"#,
                esc(id),
                if s.person == *id { "selected" } else { "" },
                esc(name(id))
            )
        })
        .collect::<String>();
    format!(
        r#"<label>Сотрудник <select data-act="ops-person"><option value="all">Все</option>{options}</select></label>"#
    )
}
fn title(group: &str, title: &str) -> String {
    format!(
        r#"<header class="view-head"><span class="eyebrow">{}</span><h1>{}</h1></header>"#,
        esc(group),
        esc(title)
    )
}
fn tile(key: &str, label: &str, total: &str, on: bool) -> String {
    format!(
        r#"<button class="stat" type="button" data-act="ops-drill" data-val="{}" data-scroll="ops-detail" aria-expanded="{}"><span class="eyebrow">{}</span><strong>{}</strong></button>"#,
        esc(key),
        on,
        esc(label),
        esc(total)
    )
}
fn empty(cols: usize) -> String {
    format!(r#"<tr><td class="muted" colspan="{cols}">За выбранный период записей нет.</td></tr>"#)
}
fn key_for(s: &State, field: &str) -> String {
    format!("ops:{}:{field}", s.route)
}
fn drill(s: &State) -> &str {
    value(s, &key_for(s, "drill"), "")
}
fn metric_range(s: &State) -> (Date, Date) {
    let base = range(s);
    let get = |field: &str| {
        s.values
            .get(&format!("ops:{}:{}:{field}", s.route, s.period))
            .and_then(|v| Date::parse(v))
    };
    (get("from").unwrap_or(base.0), get("to").unwrap_or(base.1))
}

pub fn render(s: &State) -> String {
    match s.route.as_str() {
        "funnel" | "funnel-fact" => funnel::render(s),
        "money" => finance::render(s),
        "metrics-sales" | "metrics-supply" | "metrics-logistics" => metrics::render(s),
        _ => String::new(),
    }
}
/// Whole-ruble totals in the same order as the four finance cards.
pub fn money_totals(s: &State) -> [i64; 4] {
    finance::totals(s)
}
pub fn action(s: &mut State, key: &str, v: &str) -> bool {
    match key {
        "ops-person"
            if s.role == "admin" && (v == "all" || employees().iter().any(|e| e.0 == v)) =>
        {
            s.person = v.to_owned();
            s.selected.clear();
        }
        "ops-drill" => {
            let field = key_for(s, "drill");
            if value(s, &field, "") == v {
                s.values.remove(&field);
            } else {
                s.values.insert(field, v.to_string());
            }
            s.selected.clear();
        }
        "ops-date-field" if ["created", "stage"].contains(&v) => {
            s.values.insert("ops:date-field".into(), v.into());
            s.selected.clear();
        }
        "ops-channel" if ["all", "direct", "tender"].contains(&v) => {
            s.values.insert(key_for(s, "channel"), v.into());
            s.selected.clear();
        }
        "ops-search" => {
            s.search = v.chars().take(100).collect();
            s.selected.clear();
        }
        "ops-stage" | "ops-doc" => {
            if s.selected == v {
                s.selected.clear()
            } else {
                s.selected = v.into()
            }
        }
        "ops-from" | "ops-to" => {
            let Some(d) = Date::parse(v) else {
                return false;
            };
            let (a, b) = metric_range(s);
            let field = if key == "ops-from" { "from" } else { "to" };
            let prefix = format!("ops:{}:{}:", s.route, s.period);
            s.values.insert(format!("{prefix}{field}"), d.iso());
            if field == "from" && d > b {
                s.values.insert(format!("{prefix}to"), d.iso());
            }
            if field == "to" && d < a {
                s.values.insert(format!("{prefix}from"), d.iso());
            }
            s.selected.clear();
        }
        "ops-money-date" => return finance::set_date(s, v),
        _ => return false,
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn date_validation_and_month_rollover() {
        assert_eq!(Date(2024, 2, 28).add(2), Date(2024, 3, 1));
        assert!(Date::parse("2026-02-29").is_none());
        assert_eq!(Date(2026, 12, 31).add(1), Date(2027, 1, 1));
    }
}
