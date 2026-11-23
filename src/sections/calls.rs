use super::*;

struct Conversation {
    subject: &'static str,
    customer: &'static str,
    owner: &'static str,
    seconds: usize,
    incoming: bool,
    secretary: bool,
    lpr: bool,
    next: bool,
    lines: &'static [(&'static str, &'static str)],
    outcome: &'static str,
}
const CASES: &[Conversation] = &[
    Conversation {
        subject: "Срок поставки опор",
        customer: "Заказчик Север",
        owner: "sales",
        seconds: 246,
        secretary: false,
        incoming: false,
        lpr: true,
        next: true,
        lines: &[
            ("Менеджер", "Подтверждаете спецификацию на шесть опор У110?"),
            (
                "Заказчик",
                "Да, размеры согласованы. Когда материал будет готов?",
            ),
            (
                "Менеджер",
                "Направлю график сегодня, а завтра в десять сверим даты.",
            ),
            ("Заказчик", "Договорились, жду документ."),
        ],
        outcome: "Согласована спецификация и назначено время сверки графика.",
    },
    Conversation {
        subject: "Подача предложения",
        customer: "Заказчик Волга",
        owner: "tender",
        seconds: 192,
        secretary: false,
        incoming: false,
        lpr: true,
        next: true,
        lines: &[
            ("Специалист", "Уточняю комплект документов для подачи."),
            ("Заказчик", "Приложите схему мачты и расчёт стоимости."),
            (
                "Специалист",
                "Сегодня направлю предложение, в пятницу свяжемся по замечаниям.",
            ),
        ],
        outcome: "Уточнён комплект документов. Запланирован повторный контакт.",
    },
    Conversation {
        subject: "Передача контакта",
        customer: "Заказчик Восток",
        owner: "sales",
        seconds: 68,
        secretary: true,
        incoming: false,
        lpr: false,
        next: false,
        lines: &[
            (
                "Менеджер",
                "Подскажите, с кем можно обсудить закупку крепежа?",
            ),
            (
                "Приёмная",
                "Руководитель сейчас на совещании. Общую почту можно найти в карточке компании.",
            ),
        ],
        outcome: "Разговор с приёмной, следующий контакт с ответственным не назначен.",
    },
    Conversation {
        subject: "Условия закупки кабеля",
        customer: "Заказчик Берег",
        owner: "supply",
        seconds: 218,
        secretary: false,
        incoming: true,
        lpr: true,
        next: true,
        lines: &[
            ("Снабжение", "Проверяем остаток по кабелю и условия оплаты."),
            ("Поставщик", "Готовы отгрузить в четверг."),
            ("Снабжение", "Зафиксирую партию, прошу направить счёт."),
        ],
        outcome: "Подтверждены остаток и план отгрузки.",
    },
    Conversation {
        subject: "График доставки",
        customer: "Заказчик Урал",
        owner: "logistics",
        seconds: 152,
        secretary: false,
        incoming: false,
        lpr: true,
        next: true,
        lines: &[
            ("Логист", "Окно доставки свободно утром?"),
            ("Получатель", "Да, площадка принимает с девяти."),
            ("Логист", "Подтвержу машину сегодня после обеда."),
        ],
        outcome: "Получено окно разгрузки.",
    },
    Conversation {
        subject: "Технический вопрос к заявке",
        customer: "Заказчик Север",
        owner: "tender_lead",
        seconds: 274,
        secretary: false,
        incoming: false,
        lpr: true,
        next: false,
        lines: &[
            (
                "Специалист",
                "Нужна дополнительная информация по защитному покрытию.",
            ),
            ("Заказчик", "Вышлю обновлённый лист требований."),
            ("Специалист", "Спасибо, ознакомимся."),
        ],
        outcome: "Запрошены технические требования. Дата следующего действия не указана.",
    },
    Conversation {
        subject: "Получение договора",
        customer: "Заказчик Волга",
        owner: "sales",
        seconds: 183,
        secretary: false,
        incoming: false,
        lpr: true,
        next: true,
        lines: &[
            ("Менеджер", "Вам поступил договор на поставку?"),
            ("Заказчик", "Да, замечаний по цене нет."),
            (
                "Менеджер",
                "Завтра в одиннадцать уточню результаты согласования.",
            ),
        ],
        outcome: "Подтверждены условия цены и последующий звонок.",
    },
];
struct Call {
    id: String,
    day: usize,
    hour: usize,
    case: &'static Conversation,
}
fn records(s: &State, person_filter: bool) -> Vec<Call> {
    let m = month_index(&s.period);
    let mut result: Vec<_> = (0..(13 + m % 5))
        .map(|i| Call {
            id: format!("ЗВ-{}-{:03}", s.period, i + 1),
            day: 1 + (i * 3 + m) % 28,
            hour: 9 + (i * 2) % 9,
            case: &CASES[(i + m) % CASES.len()],
        })
        .filter(|c| {
            if s.role != "admin" {
                return c.case.owner == viewer(s);
            }
            !person_filter || s.person.is_empty() || s.person == "all" || c.case.owner == s.person
        })
        .collect();
    result.sort_by_key(|c| std::cmp::Reverse((c.day, c.hour)));
    result
}
fn result(c: &Conversation) -> bool {
    c.seconds >= 120 && !c.secretary && c.lpr && c.next
}
fn duration(s: usize) -> String {
    format!("{}:{:02}", s / 60, s % 60)
}
pub(super) fn render(s: &State) -> String {
    let mut html = head("Звонки", "Записи разговоров");
    let owned = records(s, true);
    let successful = owned.iter().filter(|c| result(c.case)).count();
    write!(html,"<div class=\"stat-grid\"><div class=\"stat\"><small>Разговоров за период</small><strong>{}</strong></div><div class=\"stat\"><small>Результативные</small><strong>{successful}</strong></div><div class=\"stat\"><small>Общая длительность</small><strong>{} мин</strong></div></div>",owned.len(),owned.iter().map(|c|c.case.seconds).sum::<usize>()/60).unwrap();
    let mut choices = String::new();
    for (k, t) in [
        ("", "Все звонки"),
        ("out", "Исходящие"),
        ("in", "Входящие"),
        ("ok", "Результативные"),
        ("no", "Без результата"),
    ] {
        choices += &opt(k, t, &s.filter);
    }
    html += "<div class=\"toolbar\">";
    if s.role == "admin" {
        let mut people = opt("all", "Все сотрудники", &s.person);
        for (id, name, _) in employees() {
            people += &opt(id, name, &s.person);
        }
        write!(
            html,
            "<label>Сотрудник <select data-act=\"misc-calls-person\">{people}</select></label>"
        )
        .unwrap();
    }
    write!(
        html,
        "<label>Показывать <select data-act=\"misc-filter\">{choices}</select></label></div>"
    )
    .unwrap();
    html += &text_filter(s, "Клиент, сотрудник или тема разговора");
    let visible: Vec<_> = owned
        .iter()
        .filter(|c| {
            let case = c.case;
            (match s.filter.as_str() {
                "out" => !case.incoming,
                "in" => case.incoming,
                "ok" => result(case),
                "no" => !result(case),
                _ => true,
            }) && match_text(
                &s.search,
                &[case.subject, case.customer, person_name(case.owner), &c.id],
            )
        })
        .collect();
    write!(html,"<section class=\"panel\"><div class=\"panel-head\"><h2>Разговоры · {}</h2></div><div class=\"table-scroll\"><table><thead><tr><th>Дата и время</th><th>Клиент и тема</th><th>Сотрудник</th><th>Направление</th><th>Длительность</th><th>Результат</th></tr></thead><tbody>",visible.len()).unwrap();
    if visible.is_empty() {
        html += &empty(6);
    }
    for c in &visible {
        write!(html,"<tr><td>{} {:02}:15</td><td>{}<br><small>{}</small></td><td>{}</td><td>{}</td><td>{}</td><td><span class=\"chip {}\">{}</span></td></tr>",day_label(&s.period,c.day),c.hour,row_button("misc-detail",&c.id,c.case.subject),c.case.customer,person_name(c.case.owner),if c.case.incoming{"Входящий"}else{"Исходящий"},duration(c.case.seconds),if result(c.case){"good"}else{""},if result(c.case){"Результативный"}else{"Без результата"}).unwrap();
    }
    html += "</tbody></table></div></section>";
    if let Some(c) = visible.iter().find(|c| c.id == s.selected) {
        let k = format!("calls/feedback/{}", c.id);
        write!(html,"<section class=\"panel detail\"><div class=\"panel-head\"><div><div class=\"eyebrow\">Расшифровка · {}</div><h2>{}</h2></div>{}</div><p>{}</p>",esc(&c.id),c.case.subject,row_button("misc-detail",&c.id,"Закрыть"),c.case.outcome).unwrap();
        for (speaker, line) in c.case.lines {
            write!(html, "<p><strong>{speaker}</strong> · {line}</p>").unwrap();
        }
        write!(html,"<details><summary>Как получена оценка</summary><p>Разговор от 2 минут · {}. Контакт с секретарём · {}. Есть информация о ЛПР · {}. Следующий шаг · {}.</p></details>",if c.case.seconds>=120{"Да"}else{"Нет"},if c.case.secretary{"Да"}else{"Нет"},if c.case.lpr{"Да"}else{"Нет"},if c.case.next{"Да"}else{"Нет"}).unwrap();
        if s.role == "admin" {
            write!(html,"<form class=\"toolbar\" data-act=\"misc-call-feedback\"><label>Комментарий руководителя <input data-value-field maxlength=\"300\" value=\"{}\"></label><button class=\"btn\">Сохранить</button></form>",esc(setting(s,&k))).unwrap();
        } else if let Some(note) = s.values.get(&k) {
            write!(html, "<p>Комментарий руководителя · {}</p>", esc(note)).unwrap();
        }
        html += "</section>";
    }
    html
}
pub(super) fn action(s: &mut State, key: &str, v: &str) -> bool {
    if key == "misc-calls-person"
        && s.role == "admin"
        && (v == "all" || employees().iter().any(|e| e.0 == v))
    {
        s.person = v.into();
        s.selected.clear();
        return true;
    }
    if key != "misc-call-feedback"
        || s.role != "admin"
        || !records(s, true).iter().any(|c| c.id == s.selected)
    {
        return false;
    }
    let k = format!("calls/feedback/{}", s.selected);
    if v.trim().is_empty() {
        s.values.remove(&k);
    } else {
        s.values.insert(k, v.trim().chars().take(300).collect());
    }
    true
}
pub(super) fn answer(s: &State, q: &str) -> Option<(String, &'static str)> {
    if !allowed(s, "calls")
        || !(q.contains("звон") || q.contains("звонк") || q.contains("разговор"))
    {
        return None;
    }
    let rows = records(s, false);
    let ok = rows.iter().filter(|c| result(c.case)).count();
    let count = rows.len();
    let pct = if count == 0 {
        0
    } else {
        (100 * ok + count / 2) / count
    };
    Some((format!("За {} в доступном реестре {} разговоров, из них результативных {} ({}%). Общая длительность — {} мин.",s.period,count,ok,pct,rows.iter().map(|c|c.case.seconds).sum::<usize>()/60),"calls"))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn result_requires_all_four_criteria() {
        assert!(result(&CASES[0]));
        assert!(!result(&CASES[2]));
        assert!(result(&CASES[3]));
        assert!(!result(&CASES[5]));
    }
}
