use super::*;
use crate::http_contract as api;
use std::sync::OnceLock;

struct Partner {
    inn: &'static str,
    item: &'static str,
    price: i64,
}
const SUPPLIERS: [Partner; 6] = [
    Partner {
        inn: "0000000101",
        item: "Опора У110",
        price: 82400,
    },
    Partner {
        inn: "0000000102",
        item: "Балка двутавровая",
        price: 61200,
    },
    Partner {
        inn: "0000000103",
        item: "Мачта освещения",
        price: 146100,
    },
    Partner {
        inn: "0000000104",
        item: "Кабель силовой",
        price: 1460,
    },
    Partner {
        inn: "0000000105",
        item: "Комплект крепежа М24",
        price: 3720,
    },
    Partner {
        inn: "0000000106",
        item: "Изоляционная муфта",
        price: 4820,
    },
];
impl Partner {
    fn name(&self) -> &str {
        static FORMATTED: OnceLock<Vec<api::Supplier>> = OnceLock::new();
        FORMATTED
            .get_or_init(|| {
                let d = api::fixture();
                api::suppliers(d, "", d.snapshot_at)
            })
            .iter()
            .find(|r| r.get("Реквизит: ИНН") == self.inn)
            .map(|r| r.get("Название"))
            .unwrap_or(self.inn)
    }
}
struct Line {
    doc: String,
    day: usize,
    partner: usize,
    qty: i64,
    price: i64,
    previous: i64,
    returned: bool,
}
fn lines(period: &str) -> Vec<Line> {
    let m = month_index(period);
    (0..(11 + m % 5))
        .map(|i| {
            let vendor = (i + m) % SUPPLIERS.len();
            let base = SUPPLIERS[vendor].price;
            let qty = if vendor == 3 {
                120 + i as i64 * 3
            } else {
                2 + (m + i) as i64 % 9
            };
            let price = base * (95 + (i + m) % 11) as i64 / 100;
            let returned = i == 8;
            Line {
                doc: format!("ДМ-{}-{:03}", period, i + 1),
                day: 1 + (i * 2) % 28,
                partner: vendor,
                qty: if returned { -1 } else { qty },
                price,
                previous: base,
                returned,
            }
        })
        .collect()
}

fn contact_lines(fields: &[String]) -> String {
    fields
        .iter()
        .filter(|s| !s.is_empty())
        .map(|s| esc(s))
        .collect::<Vec<_>>()
        .join(" · ")
}
fn directory(s: &State, is_carrier: bool) -> String {
    let data = api::fixture();
    let (title, group, endpoint) = if is_carrier {
        ("Перевозчики", "Логистика", "carriers")
    } else {
        ("База поставщиков", "Поставщики", "supplier-directory")
    };
    let mut html = head(title, group);
    html += &text_filter(s, "Наименование, ИНН, регион или контакты");
    let mut body = String::new();
    let mut detail = String::new();
    let count = if is_carrier {
        let rows = api::carriers(data, &s.search);
        for carrier in &rows {
            let cells = carrier.cells.iter().map(api::cell).collect::<Vec<_>>();
            let contact = contact_lines(&cells[7..11]);
            write!(
                body,
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                row_button("misc-detail", &carrier.key, &cells[0]),
                esc(&cells[5]),
                esc(&cells[6]),
                cells[1],
                contact
            )
            .unwrap();
            if carrier.key != s.selected {
                continue;
            }
            write!(detail,"<section class=\"panel detail\"><div class=\"panel-head\"><h2>{}</h2>{}</div><p>{} · ИНН {}</p><p>{contact}</p><h3>Поручения экспедитору</h3><div class=\"table-scroll\"><table><thead><tr><th>Поручение</th><th>Дата</th><th>Маршрут</th><th>Статус</th></tr></thead><tbody>",esc(&cells[0]),row_button("misc-detail",&carrier.key,"Закрыть"),esc(&cells[6]),esc(&cells[5])).unwrap();
            for doc in &carrier.docs {
                write!(
                    detail,
                    "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                    esc(doc.doc.as_deref().unwrap_or("")),
                    esc(doc.dt.as_deref().unwrap_or("")),
                    esc(doc.route.as_deref().unwrap_or("")),
                    esc(doc.status.as_deref().unwrap_or(""))
                )
                .unwrap();
            }
            detail += "</tbody></table></div></section>";
        }
        rows.len()
    } else {
        let rows = api::suppliers(data, &s.search, data.snapshot_at);
        for row in &rows {
            let inn = row.get("Реквизит: ИНН");
            let contacts = [
                "Рабочий телефон",
                "Мобильный телефон",
                "Рабочий e-mail",
                "Корпоративный сайт",
            ]
            .iter()
            .map(|k| row.get(k).to_owned())
            .collect::<Vec<_>>();
            let contact = contact_lines(&contacts);
            write!(
                body,
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                row_button("misc-detail", &row.id, row.get("Название")),
                esc(inn),
                esc(row.get("Регион")),
                esc(row.get("Теги отдела снабжения")),
                contact
            )
            .unwrap();
            if row.id != s.selected {
                continue;
            }
            write!(detail,"<section class=\"panel detail\"><div class=\"panel-head\"><h2>{}</h2>{}</div><p>{} · ИНН {}</p><p>{contact}</p>",esc(row.get("Название")),row_button("misc-detail",&row.id,"Закрыть"),esc(row.get("Регион")),esc(inn)).unwrap();
            if let Some(index) = SUPPLIERS.iter().position(|p| p.inn == inn) {
                let sum = lines(&s.period)
                    .iter()
                    .filter(|l| l.partner == index)
                    .map(|l| l.qty * l.price)
                    .sum();
                write!(
                    detail,
                    "<p>Закупки за период <strong>{}</strong></p>{}",
                    money(sum),
                    row_button("misc-vendor-ledger", inn, "Посмотреть закупки")
                )
                .unwrap();
            }
            detail += "</section>";
        }
        rows.len()
    };
    let q = api::query_encode(&s.search);
    write!(html,"<section class=\"panel\"><div class=\"panel-head\"><h2>Контрагенты · {count}</h2><a class=\"btn\" download=\"{endpoint}.xlsx\" href=\"/api/{endpoint}.xlsx?search={q}\">Скачать Excel</a></div><div class=\"table-scroll\"><table><thead><tr><th>Наименование</th><th>ИНН</th><th>Регион</th><th>{}</th><th>Контакты</th></tr></thead><tbody>",if is_carrier{"Поручений"}else{"Специализация"}).unwrap();
    html += &body;
    if count == 0 {
        html += &empty(5);
    }
    html += "</tbody></table></div></section>";
    html += &detail;
    html
}
fn ledger(s: &State, request: bool) -> String {
    let all = lines(&s.period);
    let title = if request {
        "Заявки поставщикам"
    } else {
        "Закупки у поставщиков"
    };
    let mut html = head(title, "Поставщики");
    let mut choices = opt("", "Все поставщики", &s.filter);
    for p in &SUPPLIERS {
        choices += &opt(p.inn, p.name(), &s.filter);
    }
    write!(html,"<div class=\"toolbar\"><label>Поставщик <select data-act=\"misc-filter\">{choices}</select></label></div>").unwrap();
    html += &text_filter(s, "Документ, поставщик или номенклатура");
    let doc_id = |line: &Line| {
        if request {
            line.doc.replacen("ДМ", "ЗС", 1)
        } else {
            line.doc.clone()
        }
    };
    let displayed: Vec<_> = all
        .iter()
        .filter(|l| {
            let p = &SUPPLIERS[l.partner];
            (!request || !l.returned)
                && (s.filter.is_empty() || s.filter == "all" || s.filter == p.inn)
                && match_text(&s.search, &[p.name(), p.item, &doc_id(l)])
        })
        .collect();
    let sum: i64 = displayed.iter().map(|l| l.price * l.qty).sum();
    let unique = displayed
        .iter()
        .map(|l| l.partner)
        .collect::<std::collections::BTreeSet<_>>()
        .len();
    write!(html,"<div class=\"stat-grid\"><div class=\"stat\"><small>{}</small><strong>{}</strong></div><div class=\"stat\"><small>Документов</small><strong>{}</strong></div><div class=\"stat\"><small>Поставщиков</small><strong>{unique}</strong></div></div>",if request{"Сумма предложений без НДС"}else{"Поступления минус возвраты, без НДС"},money(sum),displayed.len()).unwrap();
    html+="<section class=\"panel\"><div class=\"panel-head\"><h2>Документы за период</h2></div><div class=\"table-scroll\"><table><thead><tr><th>Документ</th><th>Дата</th><th>Поставщик</th><th>Номенклатура</th><th>Кол-во</th><th>Сумма без НДС</th></tr></thead><tbody>";
    for line in &displayed {
        let p = &SUPPLIERS[line.partner];
        write!(
            html,
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
            row_button("misc-detail", &doc_id(line), &doc_id(line)),
            day_label(&s.period, line.day),
            p.name(),
            p.item,
            line.qty,
            money(line.qty * line.price)
        )
        .unwrap();
    }
    if displayed.is_empty() {
        html += &empty(6);
    }
    html += "</tbody></table></div></section>";
    if let Some(l) = displayed.into_iter().find(|l| doc_id(l) == s.selected) {
        let p = &SUPPLIERS[l.partner];
        let delta = l.price - l.previous;
        write!(html,"<section class=\"panel detail\"><div class=\"panel-head\"><h2>{} · {}</h2>{}</div><p>{} · {}</p><div class=\"table-scroll\"><table><thead><tr><th>Номенклатура</th><th>Цена за единицу</th><th>Предыдущая цена</th><th>Изменение</th></tr></thead><tbody><tr><td>{}</td><td>{}</td><td>{}</td><td class=\"{}\">{}{}</td></tr></tbody></table></div></section>",
            if request{"Заявка отработана"}else if l.returned{"Возврат поставщику"}else{"Приобретение товаров и услуг"},doc_id(l),row_button("misc-detail",&doc_id(l),"Закрыть"),p.name(),day_label(&s.period,l.day),p.item,money(l.price),money(l.previous),if delta>0{"bad"}else if delta<0{"good"}else{""},if delta>0{"+"}else{""},money(delta)).unwrap();
    }
    html
}
pub(super) fn action(s: &mut State, key: &str, v: &str) -> bool {
    if key == "misc-vendor-ledger" && s.route == "suppliers" && SUPPLIERS.iter().any(|p| p.inn == v)
    {
        s.route = "purchases".into();
        s.selected.clear();
        s.search.clear();
        s.filter = v.into();
        true
    } else {
        false
    }
}
pub(super) fn render(s: &State) -> String {
    match s.route.as_str() {
        "purchases" => ledger(s, false),
        "requests" => ledger(s, true),
        "carriers" => directory(s, true),
        _ => directory(s, false),
    }
}
pub(super) fn answer(s: &State, question: &str) -> Option<(String, &'static str)> {
    if question.contains("перевоз") || question.contains("поручен") {
        if !allowed(s, "carriers") {
            return None;
        }
        let data = api::fixture();
        let carriers = api::carriers(data, "");
        let orders = carriers
            .iter()
            .flat_map(|c| c.docs.iter().map(move |d| (&c.cells[0], d)))
            .filter(|(_, d)| d.dt.as_ref().is_some_and(|dt| dt.starts_with(&s.period)));
        let mut counts = std::collections::BTreeMap::new();
        for (name, _) in orders {
            *counts.entry(name.as_str().unwrap_or("")).or_insert(0usize) += 1;
        }
        let total: usize = counts.values().sum();
        let top = counts
            .iter()
            .max_by_key(|(_, n)| **n)
            .map(|(n, _)| format!(" Больше всего у {n}."))
            .unwrap_or_default();
        return Some((
            format!(
                "За {} у {} перевозчиков {} поручений.{top}",
                s.period,
                counts.len(),
                total
            ),
            "carriers",
        ));
    }
    if question.contains("закуп") || question.contains("поставщик") || question.contains("снабжен")
    {
        if !allowed(s, "suppliers") {
            return None;
        }
        let ledger = lines(&s.period);
        let sum = ledger.iter().map(|l| l.price * l.qty).sum();
        return Some((format!("За {} в реестре {} документов закупки и возвратов. Закупки без НДС с учётом возвратов составляют {}.",s.period,ledger.len(),money(sum)),"purchases"));
    }
    None
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn postings_include_receipts_and_returns() {
        let lines = lines("2026-11");
        assert!(lines.iter().any(|l| l.qty < 0));
        assert!(lines.iter().any(|l| l.qty > 0));
    }
    #[test]
    fn directory_links_keep_search_with_reserved_characters() {
        let mut state = State::default();
        state.search = "a + #".into();
        assert!(
            directory(&state, false).contains("/api/supplier-directory.xlsx?search=a%20%2B%20%23")
        );
        assert!(directory(&state, true).contains("/api/carriers.xlsx?search=a%20%2B%20%23"));
    }
}
