use super::{btn, day, month_days, month_label, month_parts, previous_month, viewer, who};
use crate::{employees, esc, State};
use std::fmt::Write;

#[derive(Clone, Copy, PartialEq)]
enum Source {
    B24,
    Onec,
}
impl Source {
    fn label(self) -> &'static str {
        match self {
            Self::B24 => "Битрикс24",
            Self::Onec => "1С",
        }
    }
}

struct Task {
    id: String,
    person: String,
    source: Source,
    title: String,
    facts: String,
    created: i64,
    due: Option<i64>,
    finished: Option<i64>,
    closed: bool,
}

// Timestamps in seconds from the start of this period. All monthly snapshots
// use the same civil time zone as the production dashboard (UTC+5).
fn time(d: i64, h: i64) -> i64 {
    (d - 1) * 86400 + h * 3600
}
fn cut(p: &str) -> i64 {
    let (y, m) = month_parts(p);
    if p == "2026-11" {
        time(23, 12)
    } else {
        i64::from(month_days(y, m)) * 86400 - 1
    }
}
fn stamp(p: &str, t: i64) -> String {
    format!(
        "{} {:02}:{:02}:{:02}",
        day(p, (t / 86400 + 1) as usize),
        (t % 86400) / 3600,
        t % 3600 / 60,
        t % 60
    )
}
fn duration(sec: i64) -> String {
    let sec = sec.unsigned_abs();
    format!(
        "{} д {:02} ч {:02} мин {:02} с",
        sec / 86400,
        sec % 86400 / 3600,
        sec % 3600 / 60,
        sec % 60
    )
}
fn default_direction(id: &str) -> &'static str {
    match id {
        "sales" | "tender" | "tender_lead" => "Продажи",
        "supply" => "Снабжение",
        "logistics" => "Логистика",
        _ => "Управление",
    }
}
fn active(s: &State, id: &str) -> bool {
    s.values
        .get(&format!("wl-member/{id}"))
        .map(|v| v != "off")
        .unwrap_or(true)
}
fn direction<'a>(s: &'a State, id: &str) -> &'a str {
    s.values
        .get(&format!("wl-direction/{id}"))
        .map(String::as_str)
        .unwrap_or_else(|| default_direction(id))
}
fn tasks(s: &State, person: &str, period: &str) -> Vec<Task> {
    let mut rows = Vec::new();
    let (_, m) = month_parts(period);
    for (pi, (id, _, _)) in employees().into_iter().enumerate() {
        if !active(s, id)
            || (person != "all" && person != id)
            || (s.role == "employee" && viewer(s) != id)
        {
            continue;
        }
        let direction = direction(s, id);
        let b24_names = match direction {
            "Снабжение" => [
                "Подтвердить срок готовности",
                "Собрать предложения поставщиков",
                "Согласовать спецификацию",
                "Проверить комплектность",
            ],
            "Логистика" => [
                "Уточнить маршрут",
                "Подтвердить погрузку",
                "Получить документы перевозчика",
                "Согласовать тариф",
            ],
            "Продажи" => [
                "Отправить коммерческое предложение",
                "Подготовить встречу",
                "Согласовать договор",
                "Обновить карточку клиента",
            ],
            _ => [
                "Утвердить план",
                "Проверить отчёт",
                "Согласовать бюджет",
                "Подтвердить сроки",
            ],
        };
        let mut add = |source: Source,
                       n: usize,
                       title: String,
                       facts: String,
                       created: i64,
                       due: Option<i64>,
                       finished: Option<i64>,
                       closed: bool| {
            rows.push(Task {
                id: format!("{id}-{}-{n}", if source == Source::B24 { "b" } else { "c" }),
                person: id.into(),
                source,
                title,
                facts,
                created,
                due,
                finished,
                closed,
            })
        };
        for i in 0..(7 + (m as usize + pi) % 5) {
            let d = (2 + (i * 3 + pi) % 17) as i64;
            let due = match i % 5 {
                4 => None,
                3 => Some(time(24, 12)),
                _ => Some(time(d + 2, 18)),
            };
            let closed = i % 4 != 2 && i % 4 != 3;
            let finished = closed.then(|| {
                due.unwrap_or(time(d, 16))
                    + if (i + pi + m as usize) % 4 == 0 {
                        3600 + 12
                    } else {
                        -7200
                    }
            });
            add(
                Source::B24,
                i,
                format!("{} · Б-{:03}", b24_names[i % 4], i + 1),
                format!(
                    "Ответственный: {} · {}",
                    who(id),
                    if closed {
                        "Завершена"
                    } else {
                        "Выполняется"
                    }
                ),
                time(d, 9),
                due,
                finished,
                closed,
            );
        }
        for i in 0..(5 + (m as usize + pi) % 4) {
            let d = (3 + (i * 2 + pi) % 16) as i64;
            let open = (i + pi) % 3 == 0;
            let late = (i + m as usize) % 3 == 1;
            let deadline = time(if open && i % 2 == 0 { 25 } else { d + 2 }, 12);
            let actual = deadline + if late { 5162 } else { -1210 };
            let (name, state, due, finished, closed) = match (direction, i % 4) {
                ("Снабжение", 0) => (
                    "Заявка в отдел снабжения",
                    if open {
                        "В работе"
                    } else {
                        "Отработана"
                    },
                    Some(deadline),
                    (!open).then_some(actual),
                    !open,
                ),
                ("Снабжение", 1) => (
                    "Заказ поставщику",
                    if open {
                        "Оплата 100%, поступление 0%"
                    } else {
                        "Оплата и поступление 100%"
                    },
                    Some(deadline),
                    (!open).then_some(actual),
                    !open,
                ),
                ("Логистика", 0 | 2) => (
                    "Заявка на расчёт ТР",
                    if open {
                        "Запрос информации"
                    } else {
                        "Отработана"
                    },
                    Some(deadline),
                    (!open).then_some(actual),
                    !open,
                ),
                ("Логистика", 1 | 3) => (
                    "Поручение экспедитору",
                    if open {
                        "ДФП не заполнена"
                    } else {
                        "ДФП заполнена"
                    },
                    (i % 2 == 1).then_some(deadline),
                    (!open).then_some(actual),
                    !open,
                ),
                ("Продажи", 0 | 2) => (
                    "Тендер",
                    if open {
                        "Действует"
                    } else {
                        "Выиграно"
                    },
                    Some(deadline),
                    (!open).then_some(actual),
                    !open,
                ),
                ("Продажи", 1) => (
                    "Коммерческое предложение",
                    if open {
                        "Черновик"
                    } else {
                        "Действует"
                    },
                    Some(deadline),
                    (!open).then_some(actual),
                    !open,
                ),
                ("Управление", 0 | 1) => (
                    "Заявка на расходование ДС",
                    if open {
                        "Списание отсутствует"
                    } else {
                        "Связано списание"
                    },
                    Some(deadline),
                    (!open).then_some(actual),
                    !open,
                ),
                ("Снабжение", _) => (
                    "Приобретение товаров и услуг",
                    "Денежная операция",
                    None,
                    Some(time(d, 11)),
                    true,
                ),
                ("Продажи", _) => (
                    "Реализация товаров и услуг",
                    "Денежная операция",
                    None,
                    Some(time(d, 11)),
                    true,
                ),
                _ => (
                    "Поступление безналичных ДС",
                    "Денежная операция",
                    None,
                    Some(time(d, 11)),
                    true,
                ),
            };
            add(
                Source::Onec,
                i,
                format!("{name} · Д-{:03}", i + 1),
                state.into(),
                time(d, if due.is_none() { 11 } else { 9 }),
                due,
                finished,
                closed,
            );
        }
    }
    rows
}
const BUCKETS: [&str; 4] = ["done", "done-late", "open", "open-late"];
const LABELS: [&str; 4] = [
    "Закрыто без просрочки",
    "Закрыто с просрочкой",
    "В работе без просрочки",
    "В работе и просрочено",
];
fn late(row: &Task, now: i64) -> bool {
    row.due.is_some_and(|d| row.finished.unwrap_or(now) > d)
        && (!row.closed || row.finished.is_some())
}
fn bucket(row: &Task, now: i64) -> usize {
    (if row.closed { 0 } else { 2 }) + usize::from(late(row, now))
}
pub fn workload_counts(s: &State, person: &str) -> [usize; 4] {
    let mut counts = [0; 4];
    for row in tasks(s, person, &s.period) {
        counts[bucket(&row, cut(&s.period))] += 1
    }
    counts
}
fn visible_source(s: &State, source: Source) -> bool {
    match s.values.get("wl-source").map(String::as_str) {
        Some("b24") => source == Source::B24,
        Some("onec") => source == Source::Onec,
        _ => true,
    }
}
fn take_filter(row: &Task, f: &str, now: i64) -> bool {
    match f {
        "closed" => row.closed,
        "working" | "active" => !row.closed,
        "overdue" | "late" => late(row, now),
        "ontime" => row.closed && row.due.zip(row.finished).is_some_and(|(d, end)| end <= d),
        _ => BUCKETS
            .iter()
            .position(|b| *b == f)
            .is_none_or(|n| bucket(row, now) == n),
    }
}
#[derive(Default)]
struct Summary {
    total: usize,
    open: usize,
    closed: usize,
    overdue: usize,
    ontime: usize,
    next: Option<i64>,
}
fn summary<'a>(rows: impl Iterator<Item = &'a Task>, now: i64) -> Summary {
    let mut a = Summary::default();
    for r in rows {
        a.total += 1;
        if r.closed {
            a.closed += 1
        } else {
            a.open += 1;
        }
        if late(r, now) {
            a.overdue += 1
        }
        if take_filter(r, "ontime", now) {
            a.ontime += 1
        }
        if !r.closed {
            if let Some(d) = r.due.filter(|date| *date >= now) {
                a.next = Some(a.next.map_or(d, |v| v.min(d)));
            }
        }
    }
    a
}

pub(super) fn render(s: &State) -> String {
    let person = if s.role == "employee" {
        viewer(s)
    } else {
        &s.person
    };
    let now = cut(&s.period);
    let rows: Vec<_> = tasks(s, person, &s.period)
        .into_iter()
        .filter(|r| visible_source(s, r.source))
        .collect();
    let prev = previous_month(&s.period);
    let past = tasks(s, person, &prev);
    let a = summary(rows.iter(), now);
    let b = summary(
        past.iter().filter(|r| visible_source(s, r.source)),
        cut(&prev),
    );
    let mut h = String::new();
    write!(h,"<div class=\"view-head\"><div><span class=\"eyebrow\">Задачи и документы</span><h1>Загрузка</h1></div>{}</div>",if s.role=="admin"{btn("wl-members","","Состав таблицы",s.selected=="wl-members")}else{String::new()}).unwrap();
    if s.selected == "wl-members" && s.role == "admin" {
        h.push_str("<section class=\"panel\"><div class=\"panel-head\"><h3>Сотрудники и направления поиска</h3></div><div class=\"table-scroll\"><table><thead><tr><th>Сотрудник</th><th>Показывать</th><th>Направление</th></tr></thead><tbody>");
        for (id, name, _) in employees() {
            write!(h,r#"<tr><td>{}</td><td>{}</td><td><select aria-label="Направление" data-act="wl-dir:{}">"#,esc(name),btn("wl-member",id,if active(s,id){"Исключить"}else{"Добавить"},false),id).unwrap();
            for d in ["Продажи", "Снабжение", "Логистика", "Управление"]
            {
                write!(
                    h,
                    "<option{}>{}</option>",
                    if direction(s, id) == d {
                        " selected"
                    } else {
                        ""
                    },
                    d
                )
                .unwrap();
            }
            h.push_str("</select></td></tr>");
        }
        h.push_str("</tbody></table></div></section>");
    }
    h.push_str("<div class=\"toolbar\">");
    let source = s.values.get("wl-source").map_or("all", String::as_str);
    for (id, label) in [("all", "Всё"), ("b24", "Битрикс24"), ("onec", "1С")] {
        h.push_str(&btn("wl-source", id, label, source == id));
    }
    h.push_str("</div><div class=\"stat-grid\">");
    for (key, label, val, previous) in [
        ("all", "Поступило", a.total, b.total),
        ("working", "В работе", a.open, b.open),
        ("closed", "Закрыто", a.closed, b.closed),
        ("overdue", "Просрочено за период", a.overdue, b.overdue),
        ("ontime", "Сдано в срок", a.ontime, b.ontime),
    ] {
        let delta = val as i64 - previous as i64;
        write!(h,r#"<button type="button" class="stat" data-act="wl-filter" data-val="{key}"><span>{label}</span><strong>{val}</strong><small>{delta:+} к прошлому месяцу</small></button>"#).unwrap();
    }
    h.push_str("</div>");
    write!(h,"<section class=\"panel\"><div class=\"panel-head\"><div><span class=\"eyebrow\">{} и {}</span><h3>Эффективность за период</h3></div><div class=\"toolbar\">",month_label(&s.period),month_label(&prev)).unwrap();
    let circle = s.values.get("wl-chart").is_some_and(|v| v == "ring");
    h.push_str(&btn("wl-chart", "bars", "Столбцы", !circle));
    h.push_str(&btn("wl-chart", "ring", "Круг", circle));
    h.push_str("</div></div>");
    let mut counts = [0usize; 4];
    let mut prior = [0usize; 4];
    for r in &rows {
        counts[bucket(r, now)] += 1;
    }
    for r in &past {
        if visible_source(s, r.source) {
            prior[bucket(r, cut(&prev))] += 1;
        }
    }
    let colors = ["#208568", "#c28b39", "#5968ad", "#cc5e58"];
    if circle && a.total > 0 {
        let mut off = 0usize;
        let mut stops = Vec::new();
        for i in 0..4 {
            let before = off;
            off += counts[i];
            stops.push(format!(
                "{} {:.2}% {:.2}%",
                colors[i],
                100. * before as f64 / a.total as f64,
                100. * off as f64 / a.total as f64
            ));
        }
        write!(h,r#"<div style="width:112px;height:112px;border-radius:50%;margin:16px auto;background:conic-gradient({});display:grid;place-items:center" role="img" aria-label="Распределение {} записей"><span style="display:grid;place-items:center;border-radius:50%;background:var(--surface,#fff);width:74px;height:74px;font-weight:bold">{}</span></div>"#,stops.join(","),a.total,a.total).unwrap();
    }
    h.push_str("<div class=\"table-scroll\"><table><thead><tr><th>Состояние</th><th>Этот месяц</th><th>Прошлый</th><th>Разница</th></tr></thead><tbody>");
    for i in 0..4 {
        let bar = if circle {
            String::new()
        } else {
            format!(
                r#"<span style="display:inline-block;vertical-align:middle;width:{}px;height:8px;border-radius:6px;background:{};margin-left:8px"></span>"#,
                counts[i] * 80 / counts.iter().max().copied().unwrap_or(1).max(1),
                colors[i]
            )
        };
        write!(
            h,
            "<tr><td>{}</td><td>{}{}</td><td>{}</td><td>{:+}</td></tr>",
            btn("wl-filter", BUCKETS[i], LABELS[i], s.filter == BUCKETS[i]),
            counts[i],
            bar,
            prior[i],
            counts[i] as i64 - prior[i] as i64
        )
        .unwrap();
    }
    h.push_str("</tbody></table></div></section>");
    if person == "all" && s.role == "admin" {
        h.push_str("<section class=\"panel\"><div class=\"panel-head\"><h3>По сотрудникам</h3></div><div class=\"table-scroll\"><table><thead><tr><th>Сотрудник</th><th>Поступило</th><th>В работе</th><th>Закрыто</th><th>Просрочено</th><th>Сдано в срок</th><th>Ближайший срок</th></tr></thead><tbody>");
        for (id, name, _) in employees().into_iter().filter(|(id, _, _)| active(s, id)) {
            let x = summary(rows.iter().filter(|r| r.person == id), now);
            write!(h,"<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",btn("wl-person",id,name,false),x.total,x.open,x.closed,x.overdue,x.ontime,x.next.map(|t|stamp(&s.period,t)).unwrap_or_else(||"—".into())).unwrap();
        }
        h.push_str("</tbody></table></div></section>");
    }
    let query = s.search.to_lowercase();
    let matched: Vec<_> = rows
        .iter()
        .filter(|r| {
            take_filter(r, &s.filter, now)
                && (query.is_empty()
                    || r.title.to_lowercase().contains(&query)
                    || who(&r.person).to_lowercase().contains(&query))
        })
        .collect();
    write!(h,"<section class=\"panel\"><div class=\"panel-head\"><div><span class=\"eyebrow\">{} записей</span><h3>Задачи и документы{}</h3></div></div><div class=\"toolbar\">",matched.len(),if person!="all"{format!(" · {}",esc(&who(person)))}else{String::new()}).unwrap();
    for (v, label) in [
        ("all", "Все записи"),
        ("working", "В работе"),
        ("closed", "Закрыто"),
        ("overdue", "Просрочено"),
        ("ontime", "Сдано в срок"),
    ] {
        h.push_str(&btn("wl-filter", v, label, s.filter == v));
    }
    write!(h,r#"<input type="search" aria-label="Поиск задачи" placeholder="Название или сотрудник" data-act="wl-search" value="{}"></div>"#,esc(&s.search)).unwrap();
    h.push_str("<div class=\"table-scroll\"><table><thead><tr><th>Документ / задача</th><th>Сотрудник</th><th>Состояние</th><th>Срок</th><th>Завершён</th><th>Отклонение от срока</th></tr></thead><tbody>");
    for r in matched {
        let delta = r.due.and_then(|d| {
            if r.closed {
                r.finished.map(|f| d - f)
            } else if d < now {
                Some(d - now)
            } else {
                None
            }
        });
        let chip = match delta {
            Some(v) if v > 0 => {
                format!("<span class=\"chip good\">Раньше на {}</span>", duration(v))
            }
            Some(v) if v < 0 => format!("<span class=\"chip bad\">Позже на {}</span>", duration(v)),
            _ => "—".into(),
        };
        write!(
            h,
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
            btn("wl-open", &r.id, &r.title, s.selected == r.id),
            esc(&who(&r.person)),
            if r.closed {
                "Закрыто"
            } else {
                "В работе"
            },
            r.due
                .map(|v| stamp(&s.period, v))
                .unwrap_or_else(|| "—".into()),
            r.finished
                .map(|v| stamp(&s.period, v))
                .unwrap_or_else(|| "—".into()),
            chip
        )
        .unwrap();
        if s.selected == r.id {
            write!(h,"<tr><td colspan=\"6\"><div class=\"detail\"><strong>{}</strong> · {}<br>Создан {} · {}</div></td></tr>",r.source.label(),esc(&r.title),stamp(&s.period,r.created),esc(&r.facts)).unwrap();
        }
    }
    h.push_str("</tbody></table></div></section>");
    h
}

pub(super) fn action(s: &mut State, key: &str, value: &str) -> bool {
    if s.route != "workload" {
        return false;
    }
    match key {
        "wl-source" if ["all", "b24", "onec"].contains(&value) => {
            s.values.insert("wl-source".into(), value.into());
            s.selected.clear();
        }
        "wl-chart" if ["bars", "ring"].contains(&value) => {
            s.values.insert("wl-chart".into(), value.into());
        }
        "wl-filter"
            if BUCKETS.contains(&value)
                || [
                    "all", "closed", "working", "active", "overdue", "late", "ontime",
                ]
                .contains(&value) =>
        {
            s.filter = value.into();
            s.selected.clear();
        }
        "wl-search" => {
            s.search = value.into();
        }
        "wl-open" => {
            s.selected = if s.selected == value {
                String::new()
            } else {
                value.into()
            };
        }
        "wl-person" if s.role == "admin" && employees().iter().any(|(p, _, _)| *p == value) => {
            s.person = value.into();
            s.selected.clear();
        }
        "wl-members" if s.role == "admin" => {
            s.selected = if s.selected == "wl-members" {
                String::new()
            } else {
                "wl-members".into()
            };
        }
        "wl-member" if s.role == "admin" && employees().iter().any(|(p, _, _)| *p == value) => {
            let mode = if active(s, value) { "off" } else { "on" };
            s.values.insert(format!("wl-member/{value}"), mode.into());
        }
        k if s.role == "admin" && k.starts_with("wl-dir:") => {
            let id = &k[7..];
            if !employees().iter().any(|(p, _, _)| *p == id)
                || !["Продажи", "Снабжение", "Логистика", "Управление"].contains(&value)
            {
                return false;
            }
            s.values.insert(format!("wl-direction/{id}"), value.into());
        }
        _ => return false,
    }
    true
}

#[cfg(test)]
mod tests {
    use super::*;
    fn task(due: Option<i64>, finished: Option<i64>, closed: bool) -> Task {
        Task {
            id: "1".into(),
            person: "x".into(),
            source: Source::B24,
            title: String::new(),
            facts: String::new(),
            created: 0,
            due,
            finished,
            closed,
        }
    }
    #[test]
    fn exact_deadline_is_on_time_and_one_second_later_is_late() {
        assert!(take_filter(&task(Some(20), Some(20), true), "ontime", 50));
        assert_eq!(bucket(&task(Some(20), Some(21), true), 50), 1);
        assert_eq!(bucket(&task(Some(20), None, false), 21), 3);
    }
    #[test]
    fn no_deadline_does_not_count_as_on_time_and_nearest_is_still_open() {
        let tasks = [
            task(None, Some(1), true),
            task(Some(40), None, false),
            task(Some(20), None, false),
            task(Some(11), Some(11), true),
        ];
        let values = summary(tasks.iter(), 30);
        assert_eq!(
            (
                values.total,
                values.open,
                values.closed,
                values.ontime,
                values.overdue,
                values.next
            ),
            (4, 2, 2, 1, 1, Some(40))
        );
    }
}
