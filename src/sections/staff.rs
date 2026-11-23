use super::*;
const SECTIONS: [(&str, &str); 10] = [
    ("kpi", "Мой KPI"),
    ("funnel", "Воронки"),
    ("calls", "Звонки"),
    ("gpt", "ttkGPT"),
    ("workload", "Загрузка"),
    ("metrics", "Метрики"),
    ("money", "Деньги"),
    ("carriers", "Перевозчики"),
    ("suppliers", "Поставщики"),
    ("staff", "Сотрудники"),
];
fn departments() -> Vec<&'static str> {
    let mut d = employees().into_iter().map(|e| e.2).collect::<Vec<_>>();
    d.sort();
    d.dedup();
    d
}
fn current_department(s: &State) -> &'static str {
    let d = departments();
    d.iter()
        .copied()
        .find(|v| *v == setting(s, "staff/department"))
        .or_else(|| d.first().copied())
        .unwrap_or("")
}
fn position(id: &str) -> &'static str {
    match id {
        "sales" => "Менеджер отдела продаж",
        "tender" => "Тендерный специалист",
        "tender_lead" => "Ведущий тендерный специалист",
        "supply" => "Специалист отдела снабжения",
        "logistics" => "Логист",
        "director" => "Директор",
        _ => "Сотрудник",
    }
}
fn acl(s: &State) -> String {
    let dept = current_department(s);
    let mut choices = String::new();
    for d in departments() {
        choices += &opt(d, d, dept);
    }
    let mut h=format!("<section class=\"panel\"><div class=\"panel-head\"><h2>Доступ к разделам</h2><label>Отдел <select data-act=\"misc-acl-department\">{choices}</select></label></div><p class=\"muted\">Перетащите раздел между списками или нажмите «Открыть» и «Скрыть».</p><div class=\"stat-grid\">");
    for visible in [true, false] {
        write!(
            h,
            "<div class=\"panel\" data-drop-key=\"{}\"><h3>{}</h3>",
            if visible { "enabled" } else { "hidden" },
            if visible {
                "Открытые разделы"
            } else {
                "Скрытые разделы"
            }
        )
        .unwrap();
        let mut count = 0;
        for (key, name) in SECTIONS {
            if section_access(s, dept, key) != visible {
                continue;
            }
            count += 1;
            write!(h,"<div class=\"panel-head\" draggable=\"true\" data-drag-key=\"{key}\"><span>{name}</span>{}</div>",
                row_button("misc-access-move",&format!("{key}|{}",if visible{"hidden"}else{"enabled"}),if visible{"Скрыть"}else{"Открыть"})).unwrap();
        }
        if count == 0 {
            h += "<p class=\"muted\">Список пуст</p>";
        }
        h += "</div>";
    }
    h += "</div></section>";
    h
}
pub(super) fn render(s: &State) -> String {
    let admin = s.role == "admin";
    let mut html = head(
        "Сотрудники",
        if admin {
            "Администрирование"
        } else {
            "Команда"
        },
    );
    html += &text_filter(s, "Имя, должность или отдел");
    let roster = employees();
    let rows = roster
        .iter()
        .filter(|e| match_text(&s.search, &[e.1, e.2, position(e.0)]))
        .collect::<Vec<_>>();
    write!(html,"<section class=\"panel\"><div class=\"panel-head\"><h2>Все пользователи · {}</h2></div><div class=\"table-scroll\"><table><thead><tr><th>Имя и фамилия</th><th>Отдел</th><th>Должность</th>{}</tr></thead><tbody>",rows.len(),if admin{"<th>Профиль KPI</th><th>Администратор</th><th>Логин</th>"}else{""}).unwrap();
    for &(id, name, depar) in &rows {
        write!(
            html,
            "<tr><td>{}</td><td>{}</td><td>{}</td>",
            row_button("misc-detail", id, name),
            esc(depar),
            position(id)
        )
        .unwrap();
        if admin {
            write!(
                html,
                "<td>{}</td><td>{}</td><td>{}</td>",
                if *id == "director" {
                    "—"
                } else {
                    position(id)
                },
                if *id == "director" { "Да" } else { "—" },
                esc(id)
            )
            .unwrap();
        }
        html += "</tr>";
    }
    if rows.is_empty() {
        html += &empty(if admin { 6 } else { 3 });
    }
    html += "</tbody></table></div></section>";
    if let Some((id, name, dept)) = roster.iter().find(|e| e.0 == s.selected) {
        write!(html,"<section class=\"panel detail\"><div class=\"panel-head\"><h2>{}</h2>{}</div><p>{} · {}</p>",esc(name),row_button("misc-detail",id,"Закрыть"),esc(dept),position(id)).unwrap();
        if admin {
            let list = SECTIONS
                .iter()
                .filter(|(k, _)| *id == "director" || section_access(s, dept, k))
                .map(|(_, v)| *v)
                .collect::<Vec<_>>()
                .join(", ");
            write!(
                html,
                "<p>Доступные разделы · {}</p>",
                if list.is_empty() {
                    "Нет открытых разделов"
                } else {
                    &list
                }
            )
            .unwrap();
        }
        html += "</section>";
    }
    if admin {
        html += &acl(s);
        let recipient = s
            .values
            .get("help/recipient")
            .map(String::as_str)
            .unwrap_or("director");
        let choices = roster
            .iter()
            .map(|e| opt(e.0, e.1, recipient))
            .collect::<String>();
        write!(html,"<section class=\"panel\"><div class=\"panel-head\"><h2>Поддержка дашборда</h2><label>Получатель обращений <select data-act=\"misc-help-recipient\">{choices}</select></label></div></section>").unwrap();
    }
    html += &crate::help::inbox(s);
    html
}
pub(super) fn action(s: &mut State, key: &str, v: &str) -> bool {
    if s.role != "admin" {
        return false;
    }
    match key {
        "misc-acl-department" if departments().contains(&v) => {
            s.values.insert("staff/department".into(), v.into());
            true
        }
        "misc-help-recipient" if employees().iter().any(|e| e.0 == v) => {
            s.values.insert("help/recipient".into(), v.into());
            true
        }
        "misc-access-move" => {
            let Some((section, to)) = v.split_once('|') else {
                return false;
            };
            if !SECTIONS.iter().any(|e| e.0 == section) || !matches!(to, "enabled" | "hidden") {
                return false;
            }
            let dept = current_department(s);
            s.values.insert(
                format!("access/{dept}/{section}"),
                if to == "enabled" { "1" } else { "0" }.into(),
            );
            true
        }
        _ => false,
    }
}
