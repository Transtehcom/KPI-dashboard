use super::*;
const MAX_MESSAGES: usize = 24;

fn ns(s: &State) -> String {
    format!("chat/{}/{}/", s.role, viewer(s))
}
fn source_name(route: &str) -> &'static str {
    match route {
        "carriers" => "Перевозчики",
        "suppliers" => "База поставщиков",
        "purchases" => "Закупки у поставщиков",
        "calls" => "Звонки",
        "kpi" => "KPI сотрудников",
        "money" => "Деньги",
        "workload" => "Загрузка",
        "staff" => "Сотрудники",
        _ => "",
    }
}
fn subject(s: &State, q: &str) -> Option<&'static str> {
    let roster = employees();
    if let Some(e) = roster.iter().find(|e| {
        e.1.to_lowercase()
            .split_whitespace()
            .any(|part| part.chars().count() > 3 && q.contains(part))
    }) {
        return if s.role == "admin" || e.0 == viewer(s) {
            Some(e.0)
        } else {
            None
        };
    }
    if s.role != "admin" {
        return roster.iter().find(|e| e.0 == viewer(s)).map(|e| e.0);
    }
    Some(
        roster
            .iter()
            .find(|e| e.0 == s.person)
            .map_or("all", |e| e.0),
    )
}
fn reply(s: &State, q: &str) -> (String, &'static str) {
    let q = q.trim().to_lowercase().replace('ё', "е");
    if let Some(reply) = calls::answer(s, &q) {
        return reply;
    }
    if let Some(reply) = partners::answer(s, &q) {
        return reply;
    }
    if q.contains("бонус") || q.contains("преми") || q.contains("кпи") || q.contains("kpi")
    {
        if allowed(s, "kpi") {
            let Some(emp) = subject(s, &q) else {
                return (
                    "В этом профиле доступны только ваши показатели.".into(),
                    "kpi",
                );
            };
            let result = if emp == "all" {
                let figures = employees()
                    .iter()
                    .filter_map(|e| crate::kpi::monthly_payout(s, e.0))
                    .collect::<Vec<_>>();
                format!("За {} к выплате по {} профилям KPI — {}. Сумма складывается из начислений по сотрудникам.",s.period,figures.len(),money(figures.iter().sum()))
            } else if let Some(amount) = crate::kpi::monthly_payout(s, emp) {
                format!(
                    "За {} начисление по KPI сотрудника {} составляет {}.",
                    s.period,
                    person_name(emp),
                    money(amount)
                )
            } else {
                format!(
                    "За {} для сотрудника {} нет расчёта KPI.",
                    s.period,
                    person_name(emp)
                )
            };
            return (result, "kpi");
        }
    }
    if q.contains("загруз")
        || q.contains("задач")
        || q.contains("эффективн")
        || q.contains("просроч")
    {
        if allowed(s, "workload") {
            let Some(emp) = subject(s, &q) else {
                return (
                    "По другим сотрудникам данные загрузки закрыты.".into(),
                    "workload",
                );
            };
            let [good, late, open, overdue] = crate::kpi::workload_counts(s, emp);
            return(format!("За {} в {} {} записей загрузки. Завершено в срок — {}, с опозданием — {}. В работе — {}, из них просрочено — {}.",s.period,if emp=="all"{"команде".into()}else{format!("профиле {}",person_name(emp))},good+late+open+overdue,good,late,open+overdue,overdue),"workload");
        }
    }
    if q.contains("денег")
        || q.contains("деньг")
        || q.contains("долг")
        || q.contains("дебитор")
        || q.contains("поступлен")
        || q.contains("оплат")
    {
        if allowed(s, "money") {
            let [debt, received, paid, planned] = crate::ops::money_totals(s);
            return(format!("За {} поступило {}, выплачено {}. Дебиторская задолженность — {}, плановые выплаты — {}.",s.period,money(received),money(paid),money(debt),money(planned)),"money");
        }
    }
    if (q.contains("сотруд") || q.contains("команд")) && allowed(s, "staff") {
        let r = employees();
        return (
            format!(
                "В справочнике {} сотрудников: {}.",
                r.len(),
                r.iter().map(|e| e.1).collect::<Vec<_>>().join(", ")
            ),
            "staff",
        );
    }
    ("Могу ответить по доступным учебным данным: звонкам, закупкам, перевозчикам, загрузке, KPI и деньгам. Уточните показатель, например «Сколько звонков за период?».".into(),"")
}

fn push(s: &mut State, text: &str) -> bool {
    let q = text.trim().chars().take(1200).collect::<String>();
    if q.is_empty() {
        return false;
    }
    let (answer, route) = reply(s, &q);
    let p = ns(s);
    let n = setting(s, &format!("{p}count"))
        .parse::<usize>()
        .unwrap_or_default()
        .min(MAX_MESSAGES);
    let idx = if n == MAX_MESSAGES {
        for i in 1..n {
            for field in ["q", "a", "r"] {
                if let Some(val) = s.values.remove(&format!("{p}{i}/{field}")) {
                    s.values.insert(format!("{p}{}/{field}", i - 1), val);
                }
            }
        }
        n - 1
    } else {
        n
    };
    for (k, v) in [("q", q), ("a", answer), ("r", route.into())] {
        s.values.insert(format!("{p}{idx}/{k}"), v);
    }
    s.values.insert(format!("{p}count"), (idx + 1).to_string());
    s.values.remove(&format!("{p}confirm"));
    true
}
pub(super) fn render(s: &State) -> String {
    let p = ns(s);
    let n = setting(s, &format!("{p}count"))
        .parse::<usize>()
        .unwrap_or_default()
        .min(MAX_MESSAGES);
    let mut h = head("ttkGPT", "Ассистент по рабочим данным");
    h += "<section class=\"panel\"><div class=\"panel-head\"><h2>Диалог</h2>";
    if n > 0 {
        h += &row_button("misc-chat-reset", "ask", "Очистить диалог");
    }
    h += "</div>";
    if setting(s, &format!("{p}confirm")) == "1" {
        write!(
            h,
            "<div class=\"toolbar\"><strong>Очистить историю этого диалога?</strong>{}{}</div>",
            row_button("misc-chat-reset", "yes", "Очистить"),
            row_button("misc-chat-reset", "no", "Отмена")
        )
        .unwrap();
    }
    if n == 0 {
        h+="<p class=\"muted\">В этой копии ассистент отвечает по учебным реестрам за выбранный период. Вопросы обрабатываются в браузере.</p><div class=\"toolbar\">";
        for (group, q) in [
            ("calls", "Сколько результативных звонков?"),
            ("workload", "Какая загрузка за период?"),
            ("kpi", "Сколько начислено по KPI?"),
            ("money", "Какая дебиторская задолженность?"),
            ("suppliers", "Сколько закупок за период?"),
        ] {
            if allowed(s, group) {
                h += &row_button("misc-chat-send", q, q);
            }
        }
        h += "</div>";
    }
    for i in 0..n {
        let q = setting(s, &format!("{p}{i}/q"));
        let a = setting(s, &format!("{p}{i}/a"));
        let route = setting(s, &format!("{p}{i}/r"));
        write!(h,"<article class=\"detail\"><div class=\"eyebrow\">Вы</div><p>{}</p><div class=\"eyebrow\">ttkGPT</div>",esc(q)).unwrap();
        if route.is_empty() || allowed(s, group(route)) {
            write!(h, "<p>{}</p>", esc(a)).unwrap();
            if !source_name(route).is_empty() {
                write!(
                    h,
                    "<a href=\"#{}\">Источник · {}</a>",
                    esc(route),
                    source_name(route)
                )
                .unwrap();
            }
        } else {
            h += "<p>Доступ к источнику закрыт для этого профиля.</p>";
        }
        h += "</article>";
    }
    h+="<form class=\"toolbar\" data-act=\"misc-chat-send\"><label>Сообщение <input data-value-field name=\"value\" placeholder=\"Спросить по данным периода\" maxlength=\"1200\" required></label><button class=\"btn\" type=\"submit\">Отправить</button></form></section>";
    h
}
pub(super) fn action(s: &mut State, key: &str, value: &str) -> bool {
    match key {
        "misc-chat-send" => push(s, value),
        "misc-chat-reset" => {
            let p = ns(s);
            let k = format!("{p}confirm");
            match value {
                "ask" => {
                    s.values.insert(k, "1".into());
                    true
                }
                "no" => {
                    s.values.remove(&k);
                    true
                }
                "yes" if setting(s, &k) == "1" => {
                    s.values.retain(|key, _| !key.starts_with(&p));
                    true
                }
                _ => false,
            }
        }
        _ => false,
    }
}
