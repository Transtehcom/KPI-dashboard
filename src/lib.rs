use chrono::{DateTime, Duration, Timelike};
use serde::{Deserialize, Serialize};
use std::cell::RefCell;
use std::collections::BTreeMap;

mod help;
mod home;
mod kpi;
mod ops;
mod sections;
mod timing;

#[derive(Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct State {
    pub route: String,
    pub period: String,
    pub person: String,
    pub role: String,
    pub selected: String,
    pub filter: String,
    pub search: String,
    pub values: BTreeMap<String, String>,
}
impl Default for State {
    fn default() -> Self {
        Self {
            route: "home".into(),
            period: "2026-11".into(),
            person: "all".into(),
            role: "admin".into(),
            selected: String::new(),
            filter: String::new(),
            search: String::new(),
            values: BTreeMap::new(),
        }
    }
}
pub fn esc(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#39;")
}
pub fn money(n: i64) -> String {
    let digits = n.unsigned_abs().to_string();
    let mut out = String::new();
    if n < 0 {
        out.push('−')
    }
    for (i, c) in digits.char_indices() {
        if i > 0 && (digits.len() - i) % 3 == 0 {
            out.push('\u{202f}')
        }
        out.push(c);
    }
    format!("{out} ₽")
}
pub fn employees() -> Vec<(&'static str, &'static str, &'static str)> {
    vec![
        ("director", "Ирина Соколова", "Администрация"),
        ("sales", "Алексей Орлов", "Отдел продаж"),
        ("tender", "Марина Белова", "Отдел продаж"),
        ("tender_lead", "Павел Крылов", "Отдел продаж"),
        ("supply", "Ольга Лебедева", "Отдел снабжения"),
        ("logistics", "Денис Власов", "Отдел логистики"),
    ]
}
pub fn viewer(s: &State) -> &str {
    if s.role == "admin" {
        "director"
    } else {
        s.values
            .get("viewer")
            .map(String::as_str)
            .filter(|v| employees().iter().any(|e| e.0 == *v && e.0 != "director"))
            .unwrap_or("sales")
    }
}
pub fn section_access(s: &State, department: &str, group: &str) -> bool {
    s.values
        .get(&format!("access/{department}/{group}"))
        .map(|v| v == "1")
        .unwrap_or(matches!(
            group,
            "kpi" | "funnel" | "calls" | "gpt" | "staff"
        ))
}
pub struct Page {
    pub path: &'static str,
    pub name: &'static str,
    pub desc: &'static str,
    pub children: &'static [(&'static str, &'static str)],
}
pub const PAGES: &[Page] = &[
    Page {
        path: "kpi",
        name: "KPI сотрудников",
        desc: "Показатели, планы и премии команды",
        children: &[],
    },
    Page {
        path: "funnel",
        name: "Воронки",
        desc: "По дате и фактическим изменениям",
        children: &[("funnel", "По дате"), ("funnel-fact", "Факт")],
    },
    Page {
        path: "calls",
        name: "Звонки",
        desc: "Записи, расшифровки и оценки",
        children: &[],
    },
    Page {
        path: "gpt",
        name: "ttkGPT",
        desc: "Ассистент по рабочим данным",
        children: &[],
    },
    Page {
        path: "workload",
        name: "Загрузка",
        desc: "Задачи и занятость сотрудников",
        children: &[],
    },
    Page {
        path: "metrics",
        name: "Метрики",
        desc: "Продажи, снабжение и логистика",
        children: &[
            ("metrics-sales", "Продажи"),
            ("metrics-supply", "Снабжение"),
            ("metrics-logistics", "Логистика"),
        ],
    },
    Page {
        path: "money",
        name: "Деньги",
        desc: "Платежи, планы и задолженность",
        children: &[],
    },
    Page {
        path: "carriers",
        name: "Перевозчики",
        desc: "Транспортные компании и перевозки",
        children: &[],
    },
    Page {
        path: "suppliers",
        name: "Поставщики",
        desc: "Поставщики, закупки и заявки",
        children: &[
            ("purchases", "Закупки"),
            ("requests", "Заявки"),
            ("suppliers", "База"),
        ],
    },
    Page {
        path: "staff",
        name: "Сотрудники",
        desc: "Команда, должности и доступ",
        children: &[],
    },
];
fn page_for(route: &str) -> Option<&'static Page> {
    PAGES
        .iter()
        .find(|p| p.path == route || p.children.iter().any(|c| c.0 == route))
}
fn permitted(s: &State, route: &str) -> bool {
    if route == "home" {
        return true;
    }
    page_for(route).is_some_and(|p| {
        s.role == "admin"
            || section_access(
                s,
                employees()
                    .into_iter()
                    .find(|e| e.0 == viewer(s))
                    .map(|e| e.2)
                    .unwrap_or(""),
                p.path,
            )
    })
}
fn go(s: &mut State, route: &str) {
    let path = if permitted(s, route) { route } else { "home" };
    s.route = if path == "metrics" {
        "metrics-sales".into()
    } else {
        path.into()
    };
    s.selected.clear();
    s.filter.clear();
    s.search.clear();
    s.person = if s.role == "admin" {
        "all".into()
    } else {
        viewer(s).to_owned()
    };
    s.values.remove("ui-dialog");
}
pub fn period_name(period: &str) -> String {
    const MONTHS: [&str; 12] = [
        "Январь",
        "Февраль",
        "Март",
        "Апрель",
        "Май",
        "Июнь",
        "Июль",
        "Август",
        "Сентябрь",
        "Октябрь",
        "Ноябрь",
        "Декабрь",
    ];
    if let Some((year, month)) = period.split_once('-') {
        if let Some(s) = month
            .parse::<usize>()
            .ok()
            .and_then(|i| i.checked_sub(1))
            .and_then(|i| MONTHS.get(i))
        {
            return format!("{s} {year}");
        }
    }
    esc(period)
}
fn apply(s: &mut State, key: &str, val: &str) {
    match key {
        "init" => {
            if let Ok(payload) = serde_json::from_str::<serde_json::Value>(val) {
                if let Some(v) = payload
                    .get("saved")
                    .and_then(|v| v.as_str())
                    .and_then(|v| serde_json::from_str::<State>(v).ok())
                {
                    *s = v;
                }
                s.values.remove("ui-dialog");
                let hour = payload
                    .get("time")
                    .and_then(|v| v.as_str())
                    .and_then(|v| DateTime::parse_from_rfc3339(v).ok())
                    .map(|v| (v.naive_utc() + Duration::hours(5)).num_seconds_from_midnight() / 60)
                    .unwrap_or(750);
                s.values.insert("local-minute".into(), hour.to_string());
                let route = payload
                    .get("route")
                    .and_then(|v| v.as_str())
                    .filter(|r| !r.is_empty())
                    .unwrap_or("home");
                go(s, route);
            }
        }
        "nav" => go(s, val),
        "theme" => {
            let next = if s.values.get("theme").is_some_and(|v| v == "dark") {
                "light"
            } else {
                "dark"
            };
            s.values.insert("theme".into(), next.into());
        }
        "period" => {
            if valid_period(val) {
                s.period = val.into();
                s.selected.clear();
            }
        }
        "session" => {
            if val == "admin" || employees().iter().any(|e| e.0 == val && val != "director") {
                s.role = if val == "admin" {
                    "admin".into()
                } else {
                    "employee".into()
                };
                s.values.insert("viewer".into(), val.into());
                s.values.remove("home-person");
                go(s, "home");
            }
        }
        "dialog" => {
            if val.is_empty() {
                s.values.remove("ui-dialog");
            } else {
                s.values.insert("ui-dialog".into(), val.into());
            }
        }
        "help-save" => help::save(s, val),
        "home-person" => {
            if s.role == "admin" && (val == "all" || employees().iter().any(|e| e.0 == val)) {
                s.values.insert("home-person".into(), val.into());
            }
        }
        "home-work" => {
            let p = home::who(s);
            go(s, "workload");
            if s.route == "workload" {
                s.person = p;
                s.filter = val.into();
                s.values.insert("wl-source".into(), "all".into());
            }
        }
        "home-pay" => {
            let p = home::who(s);
            go(s, "kpi");
            if s.route == "kpi" {
                s.person = p;
                if valid_period(val) {
                    s.period = val.into();
                }
            }
        }
        _ if permitted(s, &s.route) => match s.route.as_str() {
            "kpi" | "workload" => {
                kpi::action(s, key, val);
            }
            "money" | "funnel" | "funnel-fact" | "metrics-sales" | "metrics-supply"
            | "metrics-logistics" => {
                ops::action(s, key, val);
            }
            "home" => {}
            _ => {
                sections::action(s, key, val);
            }
        },
        _ => go(s, "home"),
    }
}
fn valid_period(v: &str) -> bool {
    v.len() == 7
        && v.get(0..5) == Some("2026-")
        && matches!(v.get(5..).and_then(|n| n.parse::<u8>().ok()), Some(1..=12))
}
fn title<'a>(s: &State, p: &'a Page) -> &'a str {
    if p.path == "kpi" && s.role != "admin" {
        "Мой KPI"
    } else {
        p.name
    }
}
fn header(s: &State) -> String {
    let mut options = format!(
        r#"<option value="admin" {}>Администратор</option>"#,
        if s.role == "admin" { "selected" } else { "" }
    );
    for (id, name, _) in employees().into_iter().filter(|e| e.0 != "director") {
        options.push_str(&format!(
            r#"<option value="{id}" {}>{name}</option>"#,
            if s.role == "employee" && viewer(s) == id {
                "selected"
            } else {
                ""
            }
        ));
    }
    let months = (1..=12)
        .map(|m| {
            let key = format!("2026-{m:02}");
            format!(
                r#"<option value="{key}" {}>{}</option>"#,
                if s.period == key { "selected" } else { "" },
                period_name(&key)
            )
        })
        .collect::<String>();
    let brand = r##"<a class="brand" data-act="nav" data-val="home" href="#home" aria-label="Транстехком · Главное меню"><img width="38" height="28" src="assets/ttk.svg" alt=""><span>транстех<b>ком</b></span><i></i><strong>Дашборд</strong></a>"##;
    let nav = if s.route == "home" {
        String::new()
    } else {
        let items = PAGES
            .iter()
            .filter(|p| permitted(s, p.path))
            .map(|p| {
                let current = page_for(&s.route).is_some_and(|active| active.path == p.path);
                let dest = p.children.first().map(|n| n.0).unwrap_or(p.path);
                format!(
                    r##"<a href="#{dest}" data-act="nav" data-val="{dest}" class="{}">{}</a>"##,
                    if current { "active" } else { "" },
                    title(s, p)
                )
            })
            .collect::<String>();
        format!(r#"<nav class="sectionnav" aria-label="Разделы">{items}</nav>"#)
    };
    format!(
        r#"<header class="mast"><div class="mast-row">{brand}<div class="head-tools"><span class="sandbox"><i></i>Тестовые данные</span><label class="session"><span>Профиль</span><select data-act="session" aria-label="Тестовый профиль">{options}</select></label><label class="period-control"><select data-act="period" aria-label="Отчётный период">{months}</select></label><button class="tool" data-act="theme" title="Сменить тему" aria-label="Сменить тему">☾</button><button class="tool help" data-act="dialog" data-val="help" title="Помощь" aria-label="Помощь">?!</button></div></div>{nav}</header>"#
    )
}
fn dialog(s: &State) -> String {
    help::dialog(s)
}
fn page_html(s: &State) -> String {
    let body = match s.route.as_str() {
        "home" => home::render(s),
        "kpi" | "workload" => kpi::render(s),
        "funnel" | "funnel-fact" | "money" | "metrics-sales" | "metrics-supply"
        | "metrics-logistics" => ops::render(s),
        _ => sections::render(s),
    };
    let sub = page_for(&s.route).map(|p| p.children).unwrap_or_default();
    let tabs = if sub.is_empty() {
        String::new()
    } else {
        format!(
            r#"<nav class="subnav" aria-label="Страницы раздела">{}</nav>"#,
            sub.iter()
                .map(|(path, name)| format!(
                    r##"<a href="#{path}" data-act="nav" data-val="{path}" class="{}">{name}</a>"##,
                    if *path == s.route { "active" } else { "" }
                ))
                .collect::<String>()
        )
    };
    format!(
        r#"{}<main class="page {}">{tabs}{body}</main>{}"#,
        header(s),
        if s.route == "home" {
            "home-page"
        } else {
            "report-page"
        },
        dialog(s)
    )
}

thread_local! {static STATE:RefCell<State>=RefCell::new(State::default()); static INPUT:RefCell<Vec<u8>>=const{RefCell::new(Vec::new())}; static OUTPUT:RefCell<Vec<u8>>=const{RefCell::new(Vec::new())};}
#[no_mangle]
pub extern "C" fn reserve(n: usize) -> *mut u8 {
    INPUT.with(|b| {
        let mut b = b.borrow_mut();
        b.resize(n, 0);
        b.as_mut_ptr()
    })
}
#[no_mangle]
pub extern "C" fn run() -> usize {
    INPUT.with(|b| {
        if let Ok((key, value)) = serde_json::from_slice::<(String, String)>(&b.borrow()) {
            STATE.with(|s| apply(&mut s.borrow_mut(), &key, &value));
        }
    });
    STATE.with(|s|{
        let s=s.borrow();
        let payload=serde_json::json!({"html":page_html(&s),"state":serde_json::to_string(&*s).unwrap_or_default(),"route":s.route,
          "theme":s.values.get("theme").map(String::as_str).unwrap_or("light"),"title":page_for(&s.route).map(|p|title(&s,p)).unwrap_or("Главное меню")});
        OUTPUT.with(|b|{let mut b=b.borrow_mut();*b=serde_json::to_vec(&payload).unwrap_or_default();b.len()})
    })
}
#[no_mangle]
pub extern "C" fn output() -> *const u8 {
    OUTPUT.with(|b| b.borrow().as_ptr())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn role_switch_scopes_routes_and_reset_filters() {
        let mut s = State::default();
        apply(&mut s, "session", "supply");
        apply(&mut s, "nav", "money");
        assert_eq!(s.route, "home");
        s.values
            .insert("access/Отдел снабжения/money".into(), "1".into());
        apply(&mut s, "nav", "money");
        assert_eq!(s.route, "money");
        assert_eq!(s.person, "supply");
    }
    #[test]
    fn labels_escape_and_money_format() {
        assert_eq!(esc("<&"), "&lt;&amp;");
        assert_eq!(money(-1234), "−1\u{202f}234 ₽");
    }
}
