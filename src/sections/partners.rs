use super::*;
#[path = "supplier_columns.rs"]
mod supplier_columns;

pub(super) struct Partner {
    pub name: &'static str,
    pub inn: &'static str,
    pub region: &'static str,
    pub category: &'static str,
    mail: &'static str,
    contact: &'static str,
    item: &'static str,
    price: i64,
}
pub(super) const SUPPLIERS: [Partner; 6] = [
    Partner {
        name: "Северная опора, ГК ООО",
        inn: "0000000101",
        region: "59-Пермский край",
        category: "Металлоконструкции",
        mail: "supply-1@example.invalid",
        contact: "Отдел поставок, доб. 101",
        item: "Опора У110",
        price: 82_400,
    },
    Partner {
        name: "Линия металла, ООО",
        inn: "0000000102",
        region: "66-Свердловская область",
        category: "Металлопрокат",
        mail: "supply-2@example.invalid",
        contact: "Отдел поставок, доб. 102",
        item: "Балка двутавровая",
        price: 61_200,
    },
    Partner {
        name: "Контур света, ТД ООО",
        inn: "0000000103",
        region: "74-Челябинская область",
        category: "Освещение",
        mail: "supply-3@example.invalid",
        contact: "Отдел поставок, доб. 103",
        item: "Мачта освещения",
        price: 146_100,
    },
    Partner {
        name: "Кабельная линия, АО",
        inn: "0000000104",
        region: "50-Московская область",
        category: "Кабельная продукция",
        mail: "supply-4@example.invalid",
        contact: "Отдел поставок, доб. 104",
        item: "Кабель силовой",
        price: 1_460,
    },
    Partner {
        name: "Формат крепежа, ПК ООО",
        inn: "0000000105",
        region: "16-Республика Татарстан",
        category: "Крепёж",
        mail: "supply-5@example.invalid",
        contact: "Отдел поставок, доб. 105",
        item: "Комплект крепежа М24",
        price: 3_720,
    },
    Partner {
        name: "Вектор покрытия, ООО",
        inn: "0000000106",
        region: "52-Нижегородская область",
        category: "Изоляция",
        mail: "supply-6@example.invalid",
        contact: "Отдел поставок, доб. 106",
        item: "Изоляционная муфта",
        price: 4_820,
    },
];
pub(super) const CARRIERS: [Partner; 3] = [
    Partner {
        name: "Восточный маршрут, ТК ООО",
        inn: "0000000201",
        region: "66-Свердловская область",
        category: "Автоперевозки",
        mail: "route-1@example.invalid",
        contact: "Диспетчерская, доб. 201",
        item: "Екатеринбург — Пермь",
        price: 74000,
    },
    Partner {
        name: "Рельсовый путь, ООО",
        inn: "0000000202",
        region: "59-Пермский край",
        category: "Железнодорожные перевозки",
        mail: "route-2@example.invalid",
        contact: "Диспетчерская, доб. 202",
        item: "Пермь — Казань",
        price: 59000,
    },
    Partner {
        name: "Волга транзит, ТК ООО",
        inn: "0000000203",
        region: "16-Республика Татарстан",
        category: "Сборные грузы",
        mail: "route-3@example.invalid",
        contact: "Диспетчерская, доб. 203",
        item: "Казань — Нижний Новгород",
        price: 48000,
    },
];
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
fn carrier_docs(period: &str, idx: usize) -> Vec<(String, usize, &'static str)> {
    let m = month_index(period);
    let n = 2 + (m + idx) % 4;
    (0..n)
        .rev()
        .map(|i| {
            (
                format!("ПЭ-ДМ-{}-{:03}", period, 10 * (idx + 1) + i),
                2 + i * 4 + idx,
                if i == n - 1 {
                    ["В пути", "Выдан", "Завершено"][idx]
                } else {
                    "Завершено"
                },
            )
        })
        .collect()
}
const CARRIER_HEADERS: [&str; 11] = [
    "Перевозчик",
    "Поручений",
    "Последнее поручение",
    "Дата поручения",
    "Маршрут последнего поручения",
    "ИНН",
    "Регион",
    "Рабочий телефон",
    "Мобильный телефон",
    "Рабочий e-mail",
    "Корпоративный сайт",
];
fn supplier_row(p: &Partner) -> Vec<String> {
    supplier_columns::COLUMNS
        .iter()
        .map(|k| {
            match *k {
                "Название" => p.name,
                "Тип компании" => "Поставщик",
                "Реквизит: ИНН" => p.inn,
                "Регион" => p.region,
                "Рабочий телефон" => p.contact,
                "Рабочий e-mail" => p.mail,
                "Теги отдела снабжения" => p.category,
                _ => "",
            }
            .to_owned()
        })
        .collect()
}
fn carrier_row(p: &Partner, idx: usize, period: &str) -> Vec<String> {
    let docs = carrier_docs(period, idx);
    vec![
        p.name.into(),
        docs.len().to_string(),
        docs[0].0.clone(),
        day_label(period, docs[0].1),
        p.item.into(),
        p.inn.into(),
        p.region.into(),
        p.contact.into(),
        String::new(),
        p.mail.into(),
        String::new(),
    ]
}
fn directory(s: &State, carrier: bool) -> String {
    let set: &[Partner] = if carrier { &CARRIERS } else { &SUPPLIERS };
    let title = if carrier {
        "Перевозчики"
    } else {
        "База поставщиков"
    };
    let mut html = head(
        title,
        if carrier {
            "Логистика"
        } else {
            "Поставщики"
        },
    );
    html += &text_filter(s, "Название, ИНН, регион или специализация");
    let seen: Vec<_> = set
        .iter()
        .enumerate()
        .filter(|(_, p)| match_text(&s.search, &[p.name, p.inn, p.region, p.category, p.mail]))
        .collect();
    let export = if carrier {
        xlsx::link(
            title,
            &CARRIER_HEADERS,
            &seen
                .iter()
                .map(|&(i, p)| carrier_row(p, i, &s.period))
                .collect::<Vec<_>>(),
        )
    } else {
        xlsx::link(
            title,
            &supplier_columns::COLUMNS,
            &seen
                .iter()
                .map(|&(_, p)| supplier_row(p))
                .collect::<Vec<_>>(),
        )
    };
    write!(html,"<section class=\"panel\"><div class=\"panel-head\"><h2>Контрагенты · {}</h2>{export}</div><div class=\"table-scroll\"><table><thead><tr><th>Наименование</th><th>ИНН</th><th>Регион</th><th>{}</th><th>Контакты</th></tr></thead><tbody>",seen.len(),if carrier{"Поручений за период"}else{"Специализация"}).unwrap();
    for &(i, p) in &seen {
        write!(
            html,
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
            row_button("misc-detail", p.inn, p.name),
            p.inn,
            p.region,
            if carrier {
                carrier_docs(&s.period, i).len().to_string()
            } else {
                p.category.into()
            },
            p.mail
        )
        .unwrap();
    }
    if seen.is_empty() {
        html += &empty(5);
    }
    html += "</tbody></table></div></section>";
    if let Some((i, p)) = seen.into_iter().find(|(_, p)| p.inn == s.selected) {
        write!(html,"<section class=\"panel detail\"><div class=\"panel-head\"><h2>{}</h2>{}</div><p>{} · ИНН {} · {}</p><p>{} · {}</p>",p.name,row_button("misc-detail",p.inn,"Закрыть"),p.region,p.inn,p.category,p.contact,p.mail).unwrap();
        if carrier {
            html+="<h3>Поручения экспедитору</h3><div class=\"table-scroll\"><table><thead><tr><th>Поручение</th><th>Дата</th><th>Маршрут</th><th>Статус</th></tr></thead><tbody>";
            for (doc, day, status) in carrier_docs(&s.period, i) {
                write!(
                    html,
                    "<tr><td>{doc}</td><td>{}</td><td>{}</td><td>{status}</td></tr>",
                    day_label(&s.period, day),
                    p.item
                )
                .unwrap();
            }
            html += "</tbody></table></div>";
        } else {
            let ledger = lines(&s.period);
            let received = ledger
                .iter()
                .filter(|l| l.partner == i)
                .map(|l| l.qty * l.price)
                .sum();
            write!(
                html,
                "<p>Закупки за период <strong>{}</strong></p>{}",
                money(received),
                row_button("misc-vendor-ledger", p.inn, "Посмотреть закупки")
            )
            .unwrap();
        }
        html += "</section>";
    }
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
        choices += &opt(p.inn, p.name, &s.filter);
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
                && match_text(&s.search, &[p.name, p.item, &doc_id(l)])
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
            p.name,
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
            if request{"Заявка отработана"}else if l.returned{"Возврат поставщику"}else{"Приобретение товаров и услуг"},doc_id(l),row_button("misc-detail",&doc_id(l),"Закрыть"),p.name,day_label(&s.period,l.day),p.item,money(l.price),money(l.previous),if delta>0{"bad"}else if delta<0{"good"}else{""},if delta>0{"+"}else{""},money(delta)).unwrap();
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
        let count: usize = (0..CARRIERS.len())
            .map(|i| carrier_docs(&s.period, i).len())
            .sum();
        return Some((
            format!(
                "За {} у {} перевозчиков {} поручений. Больше всего у {}.",
                s.period,
                CARRIERS.len(),
                count,
                CARRIERS
                    .iter()
                    .enumerate()
                    .max_by_key(|(i, _)| carrier_docs(&s.period, *i).len())
                    .unwrap()
                    .1
                    .name
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
    fn supplier_schema_and_returns() {
        assert_eq!(supplier_columns::COLUMNS.len(), 81);
        let p = supplier_row(&SUPPLIERS[0]);
        assert_eq!(p[80], "0000000101");
        assert!(p[52].is_empty());
        let l = lines("2026-11");
        assert!(l.iter().any(|l| l.qty < 0));
        assert!(l.iter().any(|l| l.qty > 0));
    }
    #[test]
    fn latest_carrier_doc_is_first() {
        for i in 0..CARRIERS.len() {
            let d = carrier_docs("2026-11", i);
            assert!(d.windows(2).all(|p| p[0].1 > p[1].1));
        }
    }
}
