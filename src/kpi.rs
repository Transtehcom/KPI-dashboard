use crate::{employees, esc, money, State};
use std::fmt::Write;

#[path = "kpi_delivery.rs"]
mod delivery;
#[path = "kpi_rules.rs"]
mod rules;
#[path = "kpi_workload.rs"]
mod workload;
pub use workload::workload_counts;

pub(super) struct Spec {
    person: &'static str,
    code: &'static str,
    title: &'static str,
    target: &'static str,
    weight: i64,
    start: &'static str,
    end: &'static str,
}

fn rules_for<'a>(person: &'a str, period: &'a str) -> impl Iterator<Item = &'static Spec> + 'a {
    rules::RULES.iter().filter(move |r| {
        r.person == person
            && (r.start.is_empty() || r.start <= period)
            && (r.end.is_empty() || period <= r.end)
    })
}

pub(super) fn viewer(s: &State) -> &str {
    crate::viewer(s)
}

fn person(s: &State) -> &str {
    if s.role == "employee" {
        viewer(s)
    } else {
        &s.person
    }
}

pub(super) fn month_parts(p: &str) -> (i32, u32) {
    p.split_once('-')
        .and_then(|(y, m)| Some((y.parse().ok()?, m.parse().ok()?)))
        .filter(|(y, m)| (2000..=2100).contains(y) && (1..=12).contains(m))
        .unwrap_or((2026, 11))
}

pub(super) fn month_label(p: &str) -> String {
    let (y, m) = month_parts(p);
    format!(
        "{} {y}",
        [
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
            "Декабрь"
        ][(m - 1) as usize]
    )
}

pub(super) fn previous_month(p: &str) -> String {
    let (y, m) = month_parts(p);
    if m == 1 {
        format!("{}-12", y - 1)
    } else {
        format!("{y}-{:02}", m - 1)
    }
}

pub(super) fn month_days(y: i32, m: u32) -> u32 {
    match m {
        2 if y % 4 == 0 && (y % 100 != 0 || y % 400 == 0) => 29,
        2 => 28,
        4 | 6 | 9 | 11 => 30,
        _ => 31,
    }
}

// Day of the Gregorian week from Monday = 0.
fn weekday(y: i32, m: u32, d: u32) -> u32 {
    let t = [0, 3, 2, 5, 0, 3, 5, 1, 4, 6, 2, 4];
    let yy = if m < 3 { y - 1 } else { y };
    ((yy + yy / 4 - yy / 100 + yy / 400 + t[(m - 1) as usize] + d as i32 + 6) % 7) as u32
}

fn working_days(period: &str) -> usize {
    let (y, m) = month_parts(period);
    // Public 2026 holiday calendar from config.example.json.
    (1..=month_days(y, m))
        .filter(|&d| {
            let holiday = y == 2026
                && matches!(
                    (m, d),
                    (1, 1 | 2 | 5..=9)
                        | (2, 23)
                        | (3, 9)
                        | (5, 1 | 11)
                        | (6, 12)
                        | (11, 4)
                        | (12, 31)
                );
            weekday(y, m, d) < 5 && !holiday
        })
        .count()
}

#[derive(Clone, Copy, PartialEq)]
enum Measure {
    Share(u32),
    LateShare(u32),
    Count(usize),
    Turnover(i64),
    Average,
    Worst(i64),
    NoFailures,
    Conversion(u32),
}

struct Row {
    title: String,
    note: String,
    auto: bool,
    value: i64,
    pending: bool,
    unknown: bool,
}
struct Group {
    title: &'static str,
    source: &'static str,
    measure: Measure,
    rows: Vec<Row>,
}

fn group(title: &'static str, source: &'static str, measure: Measure) -> Group {
    Group {
        title,
        source,
        measure,
        rows: Vec::new(),
    }
}
fn row(title: String, note: String, auto: bool, value: i64) -> Row {
    Row {
        title,
        note,
        auto,
        value,
        pending: false,
        unknown: false,
    }
}

fn doc(prefix: &str, period: &str, i: usize) -> String {
    let (y, m) = month_parts(period);
    format!("{prefix} Д-{y}{m:02}-{:03}", i + 1)
}
fn day(period: &str, d: usize) -> String {
    let (y, m) = month_parts(period);
    format!("{d:02}.{m:02}.{y}")
}

fn groups(spec: &Spec, period: &str) -> Vec<Group> {
    let (_, m) = month_parts(period);
    let salt = (m as usize + spec.person.len()) % 7;
    let good = |i: usize| (i + salt) % 5 != 0;
    let mk = |title, source, measure, label: &str, n: usize| {
        let mut g = group(title, source, measure);
        for i in 0..n {
            g.rows.push(row(
                doc(label, period, i),
                day(period, i % 21 + 1),
                good(i),
                1,
            ))
        }
        g
    };
    match spec.code {
        "logistics_delivery" => vec![delivery::logistics(period, false)],
        "logistics_budget" => vec![delivery::logistics(period, true)],
        "on_time_delivery" => vec![delivery::supply(period)],
        "logistics_request_speed" => {
            let mut g = group("Заявки, отработанные за 24 часа", "1С", Measure::Share(95));
            let (year, month) = month_parts(period);
            let ym = format!("{year}-{month:02}");
            let now = format!("{ym}-23T12:00:00");
            for (i, elapsed) in [
                Some(32_900),
                Some(86_400),
                Some(86_401),
                Some(19_240),
                Some(39_401),
                Some(27_100),
                Some(72_000),
                None,
            ]
            .into_iter()
            .enumerate()
            {
                let arrived = if elapsed.is_some() { i + 1 } else { 23 };
                let mut history = vec![[
                    "Поступила".to_owned(),
                    format!("{ym}-{arrived:02}T09:00:00"),
                ]];
                if let Some(seconds) = elapsed {
                    let end = seconds + 9 * 3600;
                    history.push([
                        "Отработана".into(),
                        format!(
                            "{ym}-{:02}T{:02}:{:02}:{:02}",
                            arrived + end / 86400,
                            end % 86400 / 3600,
                            end % 3600 / 60,
                            end % 60
                        ),
                    ]);
                }
                let status = if elapsed.is_some() {
                    "Отработана"
                } else {
                    "В работе"
                };
                let request = crate::timing::Request {
                    history: Some(serde_json::json!(history).to_string()),
                    status: status.into(),
                    now: now.clone(),
                    processing_minutes: 1440,
                };
                if let Ok(timing) = crate::timing::request_timing(request) {
                    let mut record = row(
                        doc("Заявка ТР", period, i),
                        format!(
                            "Поступила {} · Отработана {} · {} · {}",
                            timing.received.unwrap_or_default().replace('T', " "),
                            timing
                                .finished
                                .unwrap_or_else(|| "—".into())
                                .replace('T', " "),
                            timing.duration,
                            timing.result
                        ),
                        timing.category == "good",
                        1,
                    );
                    record.pending = timing.category == "pending";
                    g.rows.push(record);
                }
            }
            vec![g]
        }
        "logistics_cargo_safety" => {
            let mut g = group(
                "Средняя оценка руководителя",
                "Руководитель",
                Measure::Average,
            );
            let (y, m) = month_parts(period);
            for (i, thursday) in (1..=month_days(y, m))
                .filter(|&d| weekday(y, m, d) == 3)
                .enumerate()
            {
                let monday = chrono::NaiveDate::from_ymd_opt(y, m, thursday).unwrap()
                    - chrono::Duration::days(3);
                let score = [100, 80, 95, 90, 85][i];
                g.rows.push(row(
                    format!("Неделя с {}", monday.format("%d.%m.%Y")),
                    "Понедельная оценка руководителя".into(),
                    true,
                    score,
                ));
            }
            vec![g]
        }
        "logistics_documents" => {
            let mut g = group("Поручения с договорным файлом", "1С", Measure::Share(98));
            for i in 0..8 {
                let file = if good(i) {
                    ["Договор заявка.pdf", "Дог. на перевозку.pdf", "Договор.pdf"][i % 3]
                } else {
                    "Маршрутный лист.pdf"
                };
                g.rows.push(row(
                    doc("Поручение экспедитору", period, i),
                    format!("{} · Присоединённые файлы: {file}", day(period, 1 + i * 2)),
                    good(i),
                    1,
                ));
            }
            vec![g]
        }
        "deferred_payment" | "competitive_purchases" => {
            let deferred = spec.code == "deferred_payment";
            let mut g = group(
                if deferred {
                    "Заказы с отсрочкой"
                } else {
                    "Конкурентные и обоснованные закупки"
                },
                "1С + руководитель",
                Measure::Share(80),
            );
            for (i, amount) in [
                280_100, 499_999, 500_000, 635_900, 850_000, 515_000, 720_600, 610_000,
            ]
            .into_iter()
            .enumerate()
            {
                if amount < 500_000 {
                    continue;
                }
                let days = if good(i) { 14 + i } else { 7 };
                let pct = if i == 5 { 20 } else { 40 };
                let n = if good(i) { 3 } else { 2 };
                let reason = i == 5;
                let details = if deferred {
                    format!("отсрочка {pct}% на {days} дн.")
                } else {
                    format!(
                        "предложений: {n} · обоснование: {}",
                        if reason { "есть" } else { "нет" }
                    )
                };
                g.rows.push(row(
                    doc("Заказ поставщику", period, i),
                    format!("{} без НДС · {details}", money(amount)),
                    if deferred {
                        days >= 14 && pct >= 30
                    } else {
                        n >= 3 || reason
                    },
                    1,
                ));
            }
            vec![g]
        }
        "exec_discipline" => vec![
            mk(
                "Документооборот",
                "1С",
                Measure::Share(100),
                "Заказ с договором",
                6,
            ),
            mk(
                "Эффективность задач за месяц",
                "Битрикс24",
                Measure::Share(100),
                "Задача",
                10,
            ),
        ],
        "conversion_kp_tenders" | "conversion_tender" | "conversion_sales" | "supply_funnel" => {
            let goal = if spec.code == "conversion_sales" && period < "2026-10" {
                7
            } else {
                10
            };
            let mut g = group("Конверсия за период", "1С", Measure::Conversion(goal));
            let supply = spec.code == "supply_funnel";
            for i in 0..32 {
                let label = if supply {
                    "Заявка в снабжение"
                } else if i % 4 == 0 {
                    "Тендер"
                } else {
                    "КП"
                };
                g.rows.push(row(
                    doc(label, period, i),
                    format!("{} · основание конверсии", day(period, i % 20 + 1)),
                    true,
                    0,
                ));
            }
            for i in 0..4 {
                g.rows.push(row(
                    doc("Заказ клиента", period, i),
                    if good(i) {
                        "В закуп"
                    } else {
                        "Черновик"
                    }
                    .into(),
                    good(i),
                    1,
                ));
            }
            vec![g]
        }
        "gross_profit" => {
            let members = employees()
                .iter()
                .filter(|e| matches!(e.0, "sales" | "tender" | "tender_lead"))
                .count() as i64;
            let mut g = group(
                "Заказы клиентов «В закуп»",
                "1С",
                Measure::Turnover(150_000_000 / members.max(1)),
            );
            for i in 0..9 {
                let amount = 2_485_300 + (i as i64 + 1) * 729_150;
                let pass = good(i);
                g.rows.push(row(
                    doc("Заказ клиента", period, i),
                    format!(
                        "{} · {} · {}",
                        day(period, 1 + i * 2),
                        money(amount),
                        if pass {
                            "В закуп"
                        } else {
                            "Черновик"
                        }
                    ),
                    pass,
                    amount,
                ));
            }
            vec![g]
        }
        "qualified_leads" => vec![mk(
            "Новые целевые лиды",
            "Битрикс24",
            Measure::Count(10),
            "Лид",
            11,
        )],
        "base_development" if spec.person == "tender_lead" => vec![mk(
            "Новые активные клиенты",
            "Битрикс24",
            Measure::Count(7),
            "Карточка компании",
            9,
        )],
        "base_development" | "operational_activity" => {
            let sales = spec.person == "sales";
            let days = working_days(period);
            let call_goal = days * if sales { 15 } else { 3 };
            let kp_goal = if sales { days * 15 } else { 105 };
            let mut calls = mk(
                "Результативные звонки",
                "Битрикс24",
                Measure::Count(call_goal),
                "Звонок",
                call_goal.saturating_sub(2),
            );
            for (i, call) in calls.rows.iter_mut().enumerate() {
                let seconds = if good(i) { 133 + i % 210 } else { 67 + i % 48 };
                let lpr = i % 13 != 0;
                let next_step = i % 17 != 0;
                let repeated = !sales && i % 19 == 0;
                call.auto = seconds >= 120 && lpr && next_step && !repeated;
                call.note = format!(
                    "{} · {} мин {:02} с · {} · {}{}",
                    day(period, i % 21 + 1),
                    seconds / 60,
                    seconds % 60,
                    if lpr {
                        "ЛПР подтверждён"
                    } else {
                        "ЛПР не установлен"
                    },
                    if next_step {
                        "Следующее дело назначено в течение 24 часов"
                    } else {
                        "Нет следующего дела"
                    },
                    if repeated {
                        " · ранее учтён звонок тому же контакту в этот день"
                    } else {
                        ""
                    }
                );
            }
            let mut meetings = mk(
                "Встречи с протоколом и резюме",
                "Битрикс24",
                Measure::Count(if sales { 5 } else { 3 }),
                "Встреча",
                if sales { 6 } else { 4 },
            );
            for (i, meeting) in meetings.rows.iter_mut().enumerate() {
                let protocol = i % 4 != 0;
                let summary = i % 5 != 0;
                meeting.auto = protocol && summary;
                meeting.note = format!(
                    "{} · завершена · протокол {} · резюме {}",
                    day(period, i * 3 + 2),
                    if protocol {
                        "прикреплён"
                    } else {
                        "отсутствует"
                    },
                    if summary {
                        "заполнено"
                    } else {
                        "отсутствует"
                    }
                );
            }
            let mut offers = mk(
                "Подтверждённые коммерческие предложения",
                "1С и исходящие письма",
                Measure::Count(kp_goal),
                "КП",
                kp_goal.saturating_sub(4),
            );
            for (i, offer) in offers.rows.iter_mut().enumerate() {
                offer.note = format!(
                    "{} · {}{}",
                    day(period, i % 21 + 1),
                    if offer.auto {
                        format!("Письмо Исх-Д-{:04}", i + 1)
                    } else {
                        "Исходящее письмо отсутствует".into()
                    },
                    if sales {
                        ""
                    } else {
                        " · вне тендера"
                    }
                );
            }
            vec![calls, meetings, offers]
        }
        "tender_operations" => vec![mk(
            "Тендеры без отклонений",
            "1С",
            Measure::NoFailures,
            "Тендер",
            6,
        )],
        "receivables_overdue" | "receivables_pm" => {
            let mut g = group(
                "Максимальная просрочка оплаты",
                "1С",
                Measure::Worst(if spec.code == "receivables_pm" { 14 } else { 7 }),
            );
            for (i, days) in [3, 0, 12, 5].into_iter().enumerate() {
                g.rows.push(row(
                    doc("Заказ клиента", period, i),
                    format!(
                        "Просрочка {days} раб. дн. · остаток {}",
                        money(11000 * (i as i64 + 1))
                    ),
                    true,
                    days,
                ));
            }
            vec![g]
        }
        "tender_receivables" => vec![mk(
            "Возвращение обеспечения без штрафов",
            "1С",
            Measure::Share(100),
            "Обеспечение",
            5,
        )],
        _ => vec![],
    }
}

fn prefix(s: &State, p: &str, code: &str) -> String {
    format!("review/{}/{p}/{code}/", s.period)
}
fn decision<'a>(s: &'a State, base: &str, g: usize, i: usize) -> &'a str {
    s.values
        .get(&format!("{base}{g}/{i}"))
        .map(String::as_str)
        .unwrap_or("auto")
}
struct Score {
    ratio: Option<f64>,
    fact: String,
}
fn score(s: &State, base: &str, gi: usize, g: &Group) -> Score {
    let mut n = 0;
    let mut good = 0usize;
    let mut total = 0i64;
    let mut worst = 0;
    let mut numerator = 0;
    let mut denom = 0;
    let mut pending = 0;
    let mut unknown = 0;
    for (i, r) in g.rows.iter().enumerate() {
        let status = decision(s, base, gi, i);
        if status == "excluded"
            || s.values
                .get(&format!("include/{base}{gi}/{i}"))
                .is_some_and(|v| v == "no")
        {
            continue;
        }
        if status == "auto" && r.unknown {
            unknown += 1;
            continue;
        }
        if status == "auto" && r.pending {
            if matches!(g.measure, Measure::LateShare(_)) {
                n += 1;
                pending += 1;
            }
            continue;
        }
        n += 1;
        let ok = match status {
            "yes" => true,
            "no" => false,
            _ => r.auto,
        };
        if ok {
            good += 1;
            let amount = s
                .values
                .get(&format!("score/{base}{gi}/{i}"))
                .and_then(|v| v.parse::<i64>().ok())
                .unwrap_or(r.value);
            total += amount;
            worst = worst.max(r.value);
            if r.value == 0 {
                denom += 1;
            } else {
                numerator += 1;
            }
        }
    }
    let (ratio, fact) = match g.measure {
        Measure::LateShare(cap) => {
            let late = n - good - pending;
            let ratio = (n != 0)
                .then(|| ((n - late) as f64 * 100. / (n as f64 * (100. - f64::from(cap)))).min(1.));
            (
                ratio,
                format!("{late} с нарушением из {n} · ожидают поставки: {pending}"),
            )
        }
        Measure::Share(t) => (
            if n == 0 {
                None
            } else {
                Some(good as f64 * 100. / (n as f64 * f64::from(t)))
            },
            format!("{good} из {n}"),
        ),
        Measure::Count(t) => (Some(good as f64 / t as f64), format!("{good} из {t}")),
        Measure::Turnover(default) => {
            let plan = s
                .values
                .get(&format!("plan/{base}"))
                .and_then(|v| v.parse::<i64>().ok())
                .filter(|v| *v > 0)
                .unwrap_or(default);
            (
                Some(total as f64 / plan as f64),
                format!("{} из {}", money(total), money(plan)),
            )
        }
        Measure::Average => (
            if n == 0 {
                None
            } else {
                Some(total as f64 / n as f64 / 100.)
            },
            if n == 0 {
                "—".into()
            } else {
                format!("{:.1} из 100", total as f64 / n as f64)
            },
        ),
        Measure::Worst(t) => (
            Some(if worst <= t {
                1.
            } else {
                t as f64 / worst as f64
            }),
            format!("{worst} раб. дн."),
        ),
        Measure::NoFailures => (
            Some(if n == good { 1. } else { 0. }),
            format!("Отклонено: {}", n - good),
        ),
        Measure::Conversion(goal) => (
            if denom == 0 {
                None
            } else {
                Some(numerator as f64 * 100. / (denom as f64 * f64::from(goal)))
            },
            format!("{numerator} заказов / {denom} оснований"),
        ),
    };
    if unknown > 0 {
        Score {
            ratio: None,
            fact: format!("{fact} · ожидают данных: {unknown}"),
        }
    } else {
        Score { ratio, fact }
    }
}
fn percent(x: f64, double: bool) -> i64 {
    let f = x.clamp(0., 1.) * 100.;
    (if double {
        (f * 10. + 1e-10).round() / 10.
    } else {
        f
    })
    .round() as i64
}
fn scored(s: &State, p: &str, r: &Spec, gs: &[Group]) -> (Option<i64>, i64) {
    let base = prefix(s, p, r.code);
    let ratios: Vec<f64> = gs
        .iter()
        .enumerate()
        .filter_map(|(i, g)| score(s, &base, i, g).ratio)
        .collect();
    if ratios.is_empty() {
        return (None, 0);
    }
    let mean = r.code == "operational_activity" || r.code == "exec_discipline";
    let ratio = if mean {
        ratios.iter().map(|v| v.min(1.)).sum::<f64>() / gs.len() as f64
    } else {
        ratios.into_iter().fold(1., f64::min)
    };
    let penalty = r.code == "receivables_overdue"
        && s.values
            .get(&format!("penalty/{base}"))
            .is_some_and(|v| v == "yes");
    let pct = if penalty {
        0
    } else {
        percent(ratio, mean || r.code == "logistics_request_speed")
    };
    (Some(pct), r.weight * pct)
}
pub fn monthly_payout(s: &State, p: &str) -> Option<i64> {
    let specs: Vec<_> = rules_for(p, &s.period).collect();
    if specs.is_empty() {
        return None;
    }
    Some(
        specs
            .iter()
            .map(|r| scored(s, p, r, &groups(r, &s.period)).1)
            .sum(),
    )
}

pub(super) fn btn(key: &str, value: &str, label: &str, active: bool) -> String {
    format!(
        r#"<button type="button" class="btn{}" data-act="{}" data-val="{}">{}</button>"#,
        if active { " active" } else { "" },
        esc(key),
        esc(value),
        esc(label)
    )
}
pub(super) fn who(p: &str) -> String {
    employees()
        .into_iter()
        .find(|e| e.0 == p)
        .map(|e| e.1.to_owned())
        .unwrap_or_default()
}
fn profile_title(p: &str) -> &'static str {
    match p {
        "sales" => "Менеджер отдела продаж",
        "tender" => "Тендерный специалист",
        "tender_lead" => "Ведущий тендерный специалист",
        "supply" => "Специалист отдела снабжения",
        "logistics" => "Логист",
        _ => "Руководитель",
    }
}

fn selected_group(s: &State, n: usize) -> usize {
    s.values
        .get("kpi-group")
        .and_then(|g| g.parse().ok())
        .filter(|i| *i < n)
        .unwrap_or(0)
}

fn payout_chart(s: &State, person: &str) -> String {
    let (year, _) = month_parts(&s.period);
    let mut graph = format!(
        r#"<section class="panel"><div class="panel-head"><h3>Выплата KPI по месяцам</h3><strong>{year}</strong></div><div class="table-scroll" style="scrollbar-color:#22aaa5 transparent;scroll-behavior:smooth"><div style="display:grid;grid-template-columns:repeat(12,minmax(68px,1fr));gap:4px;min-width:860px;padding:12px;align-items:end">"#
    );
    let mut month_state = s.clone();
    for (i, label) in [
        "Янв", "Фев", "Мар", "Апр", "Май", "Июн", "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек",
    ]
    .iter()
    .enumerate()
    {
        let period = format!("{year}-{:02}", i + 1);
        month_state.period = period.clone();
        let payout = monthly_payout(&month_state, person).unwrap_or(0);
        let value = money(payout);
        let color = if s.period == period {
            "#14867e"
        } else {
            "#435997"
        };
        let height = (payout * 75 / 10_000).clamp(2, 75);
        write!(graph,r#"<button type="button" data-act="period" data-val="{period}" aria-label="{}, {value}" title="{}, {value}" style="display:flex;flex-direction:column;align-items:center;justify-content:end;gap:3px;background:transparent;border:0;color:inherit;cursor:pointer"><small>{value}</small><span aria-hidden="true" style="display:block;width:65%;height:{height}px;background:{color};border-radius:4px 4px 0 0"></span><span>{label}</span></button>"#,month_label(&period),month_label(&period)).unwrap();
    }
    graph.push_str("</div></div></section>");
    graph
}

fn detail(s: &State, p: &str, r: &Spec, gs: &[Group]) -> String {
    let mut h = String::new();
    let gi = selected_group(s, gs.len());
    let Some(g) = gs.get(gi) else { return h };
    h.push_str("<div class=\"toolbar\">");
    for (i, group) in gs.iter().enumerate() {
        h.push_str(&btn("kpi-group", &i.to_string(), group.title, i == gi));
    }
    h.push_str("</div>");
    let page = s
        .values
        .get("kpi-page")
        .and_then(|n| n.parse::<usize>().ok())
        .unwrap_or(0)
        .min(g.rows.len().saturating_sub(1) / 12);
    let base = prefix(s, p, r.code);
    let sc = score(s, &base, gi, g);
    write!(
        h,
        "<div class=\"panel-head\"><strong>{}</strong><span class=\"muted\">{} · {}</span></div>",
        g.title, g.source, sc.fact
    )
    .unwrap();
    if let Measure::Turnover(default) = g.measure {
        let saved = s
            .values
            .get(&format!("plan/{base}"))
            .map(String::as_str)
            .unwrap_or("");
        if s.role == "admin" {
            write!(h,r#"<div class="toolbar"><label>Индивидуальный план, ₽ <input type="number" min="1" step="1" placeholder="{default}" value="{}" data-act="kpi-plan"></label></div>"#,esc(saved)).unwrap();
        }
    }
    if r.code == "receivables_overdue" {
        let penalty = s
            .values
            .get(&format!("penalty/{base}"))
            .is_some_and(|v| v == "yes");
        h.push_str("<div class=\"toolbar\"><span>Штрафы за месяц</span>");
        if s.role == "admin" {
            h.push_str(&btn("kpi-penalty", "no", "Нет", !penalty));
            h.push_str(&btn("kpi-penalty", "yes", "Есть", penalty));
        } else {
            h.push_str(if penalty { "Есть" } else { "Нет" });
        }
        h.push_str("</div>");
    }
    h.push_str("<div class=\"table-scroll\"><table><thead><tr><th>Запись</th><th>Данные источника</th><th>Согласовано</th></tr></thead><tbody>");
    for (i, row) in g.rows.iter().enumerate().skip(page * 12).take(12) {
        let value = decision(s, &base, gi, i);
        let id = format!("{gi}/{i}");
        let effective = value == "yes" || value == "auto" && row.auto;
        let auto_label = if row.unknown {
            "Нет данных"
        } else if row.pending {
            "Ожидает срока"
        } else if row.auto {
            "Авто ✓"
        } else {
            "Авто ×"
        };
        let label = match value {
            "yes" => "Да",
            "no" => "Нет",
            "excluded" => "Исключено",
            _ if effective => "Авто ✓",
            _ => auto_label,
        };
        write!(
            h,
            "<tr><td><strong>{}</strong></td><td>{}",
            esc(&row.title),
            esc(&row.note)
        )
        .unwrap();
        if r.code == "logistics_budget" {
            let include = s
                .values
                .get(&format!("include/{base}{id}"))
                .is_none_or(|v| v != "no");
            if s.role == "admin" {
                write!(h,r#" <label>Учёт <select aria-label="Учитывать заказ" data-act="kpi-include:{id}"><option value="yes"{}>Да</option><option value="no"{}>Нет</option></select></label>"#,if include{" selected"}else{""},if include{""}else{" selected"}).unwrap();
            } else {
                write!(h, " · Учёт: {}", if include { "Да" } else { "Нет" }).unwrap();
            }
        }
        if g.measure == Measure::Average {
            let number = s
                .values
                .get(&format!("score/{base}{id}"))
                .cloned()
                .unwrap_or_else(|| row.value.to_string());
            if s.role == "admin" && value != "excluded" {
                write!(h,r#" <input type="number" aria-label="Оценка недели" min="0" max="100" value="{}" data-act="kpi-score:{id}">"#,esc(&number)).unwrap();
            } else {
                write!(h, " · {} из 100", esc(&number)).unwrap();
            }
        }
        h.push_str("</td><td>");
        if s.role == "admin" && value != "excluded" {
            write!(
                h,
                r#"<select aria-label="Согласование {}" data-act="kpi-review:{id}">"#,
                esc(&row.title)
            )
            .unwrap();
            for (v, n) in [("auto", auto_label), ("yes", "Да"), ("no", "Нет")] {
                write!(
                    h,
                    "<option value=\"{v}\"{}>{n}</option>",
                    if value == v { " selected" } else { "" }
                )
                .unwrap();
            }
            h.push_str("</select>");
            write!(h,r#" <button type="button" class="btn bad" aria-label="Исключить {}" data-act="kpi-ask" data-val="{id}">×</button>"#,esc(&row.title)).unwrap();
            if s.values.get("kpi-confirm") == Some(&format!("{base}{id}")) {
                h.push_str("<div class=\"detail\">Исключить эту запись из расчёта? ");
                h.push_str(&btn("kpi-remove", &id, "Исключить", false));
                h.push_str(&btn("kpi-cancel", "", "Отмена", false));
                h.push_str("</div>");
            }
        } else {
            h.push_str(label);
            if s.role == "admin" {
                h.push_str(&btn("kpi-restore", &id, "Вернуть", false));
            }
        }
        h.push_str("</td></tr>");
    }
    h.push_str("</tbody></table></div>");
    if g.rows.len() > 12 {
        h.push_str("<div class=\"toolbar\">");
        if page > 0 {
            h.push_str(&btn("kpi-page", &(page - 1).to_string(), "Назад", false));
        }
        write!(
            h,
            "<span>{}–{} из {}</span>",
            page * 12 + 1,
            ((page + 1) * 12).min(g.rows.len()),
            g.rows.len()
        )
        .unwrap();
        if (page + 1) * 12 < g.rows.len() {
            h.push_str(&btn("kpi-page", &(page + 1).to_string(), "Далее", false));
        }
        h.push_str("</div>");
    }
    h
}

pub fn render(s: &State) -> String {
    if s.route == "workload" {
        return workload::render(s);
    }
    let mut h = String::new();
    let p = person(s);
    if p == "all" || p.is_empty() {
        h.push_str("<div class=\"view-head\"><div><span class=\"eyebrow\">Команда</span><h1>KPI сотрудников</h1></div></div>");
        write!(h,"<div class=\"panel\"><div class=\"panel-head\"><strong>Показатели за месяц</strong><span class=\"muted\">{}</span></div><div class=\"table-scroll\"><table><thead><tr><th>Сотрудник</th><th>Профиль KPI</th><th>Выполнение</th><th>К выплате</th></tr></thead><tbody>",month_label(&s.period)).unwrap();
        for (id, name, _) in employees() {
            if let Some(sum) = monthly_payout(s, id) {
                write!(
                    h,
                    "<tr><td>{}</td><td>{}</td><td>{}%</td><td><strong>{}</strong></td></tr>",
                    btn("kpi-person", id, name, false),
                    profile_title(id),
                    (sum + 50) / 100,
                    money(sum)
                )
                .unwrap();
            }
        }
        h.push_str("</tbody></table></div></div>");
        return h;
    }
    let Some(total) = monthly_payout(s, p) else {
        return format!("<div class=\"view-head\"><h1>Мой KPI</h1></div><section class=\"panel\"><div class=\"panel-head\">{} · профиль KPI не назначен</div>{}</section>",esc(&who(p)),if s.role=="admin"{btn("kpi-person","all","Вся команда",false)}else{String::new()});
    };
    write!(h,"<div class=\"view-head\"><div><span class=\"eyebrow\">{}</span><h1>{}</h1></div><div class=\"toolbar\">",profile_title(p),esc(&who(p))).unwrap();
    if s.role == "admin" {
        h.push_str(&btn("kpi-person", "all", "Вся команда", false));
    }
    h.push_str(&btn("kpi-method", "", "Методика", s.selected == "method"));
    h.push_str("</div></div>");
    let rules: Vec<_> = rules_for(p, &s.period).collect();
    write!(h,"<div class=\"stat-grid\"><div class=\"stat\"><span>К выплате по KPI</span><strong>{}</strong></div><div class=\"stat\"><span>Фонд за месяц</span><strong>{}</strong></div><div class=\"stat\"><span>Период</span><strong>{}</strong></div></div>",money(total),money(10_000),month_label(&s.period)).unwrap();
    h.push_str(&payout_chart(s, p));
    if s.selected == "method" {
        h.push_str("<section class=\"panel\"><div class=\"panel-head\"><strong>Методика расчёта</strong></div><div class=\"detail\">Стоимость пункта = фонд × вес. Выплата пропорциональна целому проценту выполнения, максимум 100%. Решение руководителя заменяет автоматическую оценку записи. Исключение убирает её из расчёта.</div>");
        for (i, r) in rules.iter().enumerate() {
            write!(
                h,
                "<div class=\"detail\"><b>KPI {} · {} · {}%</b><br>{}</div>",
                i + 1,
                esc(r.title),
                r.weight,
                esc(r.target)
            )
            .unwrap();
        }
        h.push_str("</section>");
    }
    for (i, r) in rules.iter().enumerate() {
        let gs = groups(r, &s.period);
        let (pct, pay) = scored(s, p, r, &gs);
        let open = s.selected == r.code;
        write!(h,"<section class=\"panel\"><div class=\"panel-head\"><div><span class=\"eyebrow\">KPI {} · вес {}% · {}</span><h3>{}</h3></div><strong>{} · {}</strong></div><div class=\"detail\"><div>{}</div><div class=\"toolbar\">{}</div></div>",
            i+1,r.weight,money(r.weight*100),esc(r.title),pct.map_or("—".into(),|v|format!("{v}%")),money(pay),esc(r.target),btn("kpi-open",r.code,if open {"Свернуть расчёт"}else{"Как считается и из чего"},open)).unwrap();
        if open {
            h.push_str(&detail(s, p, r, &gs));
        }
        h.push_str("</section>");
    }
    h
}

fn parse_pair(id: &str) -> Option<(usize, usize)> {
    let (g, i) = id.split_once('/')?;
    Some((g.parse().ok()?, i.parse().ok()?))
}
pub fn action(s: &mut State, key: &str, value: &str) -> bool {
    if key.starts_with("wl-") {
        return workload::action(s, key, value);
    }
    if !key.starts_with("kpi-") || s.route != "kpi" {
        return false;
    }
    if key == "kpi-person"
        && s.role == "admin"
        && (value == "all" || employees().iter().any(|(id, _, _)| *id == value))
    {
        s.person = value.into();
        s.selected.clear();
        s.values.remove("kpi-confirm");
        return true;
    }
    if key == "kpi-method" {
        s.selected = if s.selected == "method" {
            String::new()
        } else {
            "method".into()
        };
        return true;
    }
    let p = person(s).to_string();
    if key == "kpi-open" && rules_for(&p, &s.period).any(|r| r.code == value) {
        s.selected = if s.selected == value {
            String::new()
        } else {
            value.into()
        };
        s.values.remove("kpi-group");
        s.values.remove("kpi-page");
        s.values.remove("kpi-confirm");
        return true;
    }
    let Some(spec) = rules_for(&p, &s.period).find(|r| r.code == s.selected) else {
        return false;
    };
    let gs = groups(spec, &s.period);
    match key {
        "kpi-group" if value.parse::<usize>().is_ok_and(|i| i < gs.len()) => {
            s.values.insert("kpi-group".into(), value.into());
            s.values.remove("kpi-page");
            s.values.remove("kpi-confirm");
            return true;
        }
        "kpi-page"
            if value
                .parse::<usize>()
                .is_ok_and(|i| i < gs[selected_group(s, gs.len())].rows.len().div_ceil(12)) =>
        {
            s.values.insert("kpi-page".into(), value.into());
            s.values.remove("kpi-confirm");
            return true;
        }
        _ => {}
    }
    if s.role != "admin" {
        return false;
    }
    if key == "kpi-plan" && spec.code == "gross_profit" {
        let plan_key = format!("plan/{}", prefix(s, &p, spec.code));
        if value.is_empty() {
            s.values.remove(&plan_key);
            return true;
        }
        if value.parse::<i64>().is_ok_and(|v| v > 0) {
            s.values.insert(plan_key, value.into());
            return true;
        }
        return false;
    }
    if key == "kpi-penalty" && spec.code == "receivables_overdue" && ["yes", "no"].contains(&value)
    {
        s.values.insert(
            format!("penalty/{}", prefix(s, &p, spec.code)),
            value.into(),
        );
        return true;
    }
    let id = key.split_once(':').map(|(_, id)| id).unwrap_or(value);
    if key == "kpi-cancel" {
        s.values.remove("kpi-confirm");
        return true;
    }
    let Some((gi, ri)) = parse_pair(id) else {
        return false;
    };
    if gi >= gs.len() || ri >= gs[gi].rows.len() {
        return false;
    }
    let base = prefix(s, &p, spec.code);
    let slot = format!("{base}{gi}/{ri}");
    let excluded = decision(s, &base, gi, ri) == "excluded";
    match key.split(':').next().unwrap_or("") {
        "kpi-include" if spec.code == "logistics_budget" && ["yes", "no"].contains(&value) => {
            if value == "no" {
                s.values.insert(format!("include/{slot}"), value.into());
            } else {
                s.values.remove(&format!("include/{slot}"));
            }
        }
        "kpi-review" if !excluded && ["auto", "yes", "no"].contains(&value) => {
            if value == "auto" {
                s.values.remove(&slot);
            } else {
                s.values.insert(slot, value.into());
            }
        }
        "kpi-score"
            if gs[gi].measure == Measure::Average
                && value.parse::<u8>().is_ok_and(|v| v <= 100) =>
        {
            s.values.insert(format!("score/{slot}"), value.into());
        }
        "kpi-ask" if !excluded => {
            s.values.insert("kpi-confirm".into(), slot);
            return true;
        }
        "kpi-remove" if !excluded && s.values.get("kpi-confirm") == Some(&slot) => {
            if let Some(old) = s.values.insert(slot.clone(), "excluded".into()) {
                s.values.insert(format!("prior/{slot}"), old);
            }
        }
        "kpi-restore" if excluded => {
            s.values.remove(&slot);
            if let Some(old) = s.values.remove(&format!("prior/{slot}")) {
                s.values.insert(slot, old);
            }
        }
        _ => return false,
    }
    s.values.remove("kpi-confirm");
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn active_profiles_have_a_full_fund_and_source_rows() {
        for p in ["sales", "supply", "tender", "tender_lead", "logistics"] {
            for month in ["2026-09", "2026-10", "2026-11"] {
                let specs: Vec<_> = rules_for(p, month).collect();
                assert_eq!(specs.iter().map(|r| r.weight).sum::<i64>(), 100);
                assert!(
                    specs.iter().all(|r| !groups(r, month).is_empty()),
                    "{p} {month}"
                );
            }
        }
    }

    #[test]
    fn speed_includes_exact_deadline_and_excludes_unexpired_pending_request() {
        let s = State::default();
        let spec = rules_for("logistics", &s.period)
            .find(|r| r.code == "logistics_request_speed")
            .unwrap();
        let gs = groups(spec, &s.period);
        let rows = &gs[0].rows;
        assert!(rows[1].auto && rows[1].note.contains("24 ч 00 мин 00 с"));
        assert!(!rows[2].auto && rows[2].note.contains("24 ч 00 мин 01 с"));
        assert!(rows.last().unwrap().pending);
        assert_eq!(score(&s, "", 0, &gs[0]).fact, "6 из 7");
        assert_eq!(scored(&s, "logistics", spec, &gs), (Some(90), 1350));
    }

    #[test]
    fn review_overrides_either_way_and_exclusion_needs_confirmation_for_same_month() {
        let mut s = State {
            route: "kpi".into(),
            person: "logistics".into(),
            selected: "logistics_delivery".into(),
            ..State::default()
        };
        let p = "logistics";
        let rule = rules_for(p, &s.period)
            .find(|r| r.code == "logistics_delivery")
            .unwrap();
        let g = groups(rule, &s.period);
        let row = g[0].rows.iter().position(|r| !r.auto).unwrap();
        let id = format!("0/{row}");
        let orig = monthly_payout(&s, p).unwrap();
        assert!(action(&mut s, &format!("kpi-review:{id}"), "yes"));
        let approved = monthly_payout(&s, p).unwrap();
        assert!(approved > orig);
        assert!(!action(&mut s, "kpi-remove", &id));
        assert!(action(&mut s, "kpi-ask", &id));
        s.period = "2026-10".into();
        assert!(!action(&mut s, "kpi-remove", &id));
        s.period = "2026-11".into();
        assert!(action(&mut s, "kpi-remove", &id));
        let base = prefix(&s, p, rule.code);
        assert_eq!(decision(&s, &base, 0, row), "excluded");
        assert!(action(&mut s, "kpi-restore", &id));
        assert_eq!(monthly_payout(&s, p), Some(approved));
        assert!(action(&mut s, &format!("kpi-review:{id}"), "no"));
        assert_eq!(monthly_payout(&s, p), Some(orig));
        s.role = "employee".into();
        s.values.insert("viewer".into(), p.into());
        assert!(!action(&mut s, &format!("kpi-review:{id}"), "yes"));
        assert!(!render(&s).contains("data-act=\"kpi-ask\""));
    }
}
