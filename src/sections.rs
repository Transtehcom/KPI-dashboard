use crate::{employees, esc, money, section_access, viewer, State};
use std::fmt::Write;

#[path = "sections/calls.rs"]
mod calls;
#[path = "sections/chat.rs"]
mod chat;
#[path = "sections/partners.rs"]
mod partners;
#[path = "sections/staff.rs"]
mod staff;
#[path = "sections/xlsx.rs"]
pub(crate) mod xlsx;
#[path = "sections/supplier_columns.rs"]
pub(crate) mod supplier_columns;

fn setting<'a>(s: &'a State, key: &str) -> &'a str {
    s.values.get(key).map(String::as_str).unwrap_or("")
}
fn match_text(search: &str, fields: &[&str]) -> bool {
    let needle = search.trim().to_lowercase();
    needle.is_empty() || fields.iter().any(|s| s.to_lowercase().contains(&needle))
}
fn person_name(id: &str) -> &'static str {
    employees()
        .into_iter()
        .find(|e| e.0 == id)
        .map(|e| e.1)
        .unwrap_or("Сотрудник")
}
fn opt(value: &str, title: &str, current: &str) -> String {
    format!(
        "<option value=\"{}\" {}>{}</option>",
        esc(value),
        if value == current { "selected" } else { "" },
        esc(title)
    )
}
fn head(title: &str, eyebrow: &str) -> String {
    format!(
        "<div class=\"view-head\"><div class=\"eyebrow\">{}</div><h1>{}</h1></div>",
        esc(eyebrow),
        esc(title)
    )
}
fn text_filter(s: &State, placeholder: &str) -> String {
    format!("<form class=\"toolbar\" data-act=\"misc-search\"><label>Поиск <input type=\"search\" data-value-field value=\"{}\" placeholder=\"{}\"></label><button class=\"btn\" type=\"submit\">Найти</button>{}</form>",
        esc(&s.search), esc(placeholder), if s.search.is_empty(){""}else{"<button type=\"button\" class=\"btn\" data-act=\"misc-search\" data-val=\"\">Сбросить</button>"})
}
fn row_button(key: &str, value: &str, label: &str) -> String {
    format!(
        "<button type=\"button\" class=\"btn\" data-act=\"{}\" data-val=\"{}\">{}</button>",
        esc(key),
        esc(value),
        esc(label)
    )
}
fn empty(columns: usize) -> String {
    format!("<tr><td colspan=\"{columns}\" class=\"muted\">По выбранным условиям записей нет.</td></tr>")
}
fn month_index(period: &str) -> usize {
    period
        .split_once('-')
        .and_then(|(_, month)| month.parse().ok())
        .filter(|v| (1..=12).contains(v))
        .unwrap_or(11)
}
fn day_label(period: &str, day: usize) -> String {
    let (year, month) = period.split_once('-').unwrap_or(("2026", "11"));
    format!("{day:02}.{}.{}", esc(month), esc(year))
}
fn group(route: &str) -> &str {
    match route {
        "purchases" | "requests" => "suppliers",
        other => other,
    }
}
fn allowed(s: &State, group: &str) -> bool {
    s.role == "admin"
        || employees()
            .into_iter()
            .find(|e| e.0 == viewer(s))
            .is_some_and(|e| section_access(s, e.2, group))
}

pub fn render(s: &State) -> String {
    if !allowed(s, group(&s.route)) {
        return "<section class=\"panel\"><h2>Раздел закрыт для вашего отдела</h2></section>"
            .into();
    }
    match s.route.as_str() {
        "suppliers" | "purchases" | "requests" | "carriers" => partners::render(s),
        "staff" => staff::render(s),
        "calls" => calls::render(s),
        "gpt" => chat::render(s),
        _ => String::new(),
    }
}

pub fn action(s: &mut State, key: &str, value: &str) -> bool {
    if !allowed(s, group(&s.route)) {
        return false;
    }
    match key {
        "misc-search" => {
            s.search = value.trim().chars().take(180).collect();
            s.selected.clear();
            true
        }
        "misc-detail" => {
            if s.selected == value {
                s.selected.clear()
            } else {
                s.selected = value.chars().take(80).collect()
            }
            true
        }
        "misc-filter" => {
            s.filter = value.chars().take(48).collect();
            s.selected.clear();
            true
        }
        _ => match s.route.as_str() {
            "staff" => staff::action(s, key, value),
            "calls" => calls::action(s, key, value),
            "gpt" => chat::action(s, key, value),
            "purchases" | "requests" | "suppliers" | "carriers" => partners::action(s, key, value),
            _ => false,
        },
    }
}
