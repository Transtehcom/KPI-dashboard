use super::*;
use crate::money;
use std::fmt::Write;

#[derive(Clone, Copy, PartialOrd, Ord, PartialEq, Eq, Debug)]
struct Moment(Date, u32);
impl Moment {
    fn show(self) -> String {
        format!("{} {:02}:{:02}", self.0.show(), self.1 / 60, self.1 % 60)
    }
}
#[derive(Copy, Clone, PartialEq, Eq)]
enum Kind {
    Quote,
    Tender,
    Supply,
    Transport,
    Freight,
}
impl Kind {
    fn label(self) -> &'static str {
        match self {
            Self::Quote => "Коммерческое предложение",
            Self::Tender => "Тендер",
            Self::Supply => "Заявка в отдел снабжения",
            Self::Transport => "Заявка на расчёт ТР",
            Self::Freight => "Поручение экспедитору",
        }
    }
}
#[derive(Clone)]
struct Item {
    product: usize,
    variant: u8,
    unit: &'static str,
    qty: i64,
    price: i64,
    supplier: &'static str,
}
struct Doc {
    no: String,
    kind: Kind,
    owner: &'static str,
    party: &'static str,
    channel: &'static str,
    started: Moment,
    due: Option<Moment>,
    closed: Option<Moment>,
    submitted: Option<Moment>,
    final_state: &'static str,
    amount: i64,
    items: Vec<Item>,
    related: String,
}
fn dataset() -> Vec<Doc> {
    let mut out = Vec::new();
    for m in 1..=11 {
        for i in 0..17_usize {
            let date = Date(2026, m, 1 + (i as u32 * 3 + m) % 27);
            if date > SNAPSHOT {
                continue;
            }
            let n = (m as usize - 1) * 17 + i;
            let owner = SELLERS[n % 3];
            let born = Moment(date, 9 * 60 + (n % 75) as u32);
            let planned = (i % 8 != 7).then_some(Moment(date.add(3), 12 * 60));
            let completed = (i % 5 != 1).then_some(Moment(
                date.add(if i % 3 == 0 { 5 } else { 2 }),
                10 * 60 + 15,
            ));
            let accepted = i % 4 != 0;
            let parent = format!("ЗК-{:04}", 1600 + n);
            let state = if accepted {
                "Действует"
            } else {
                "Отменено"
            };
            let sku = n % PRODUCTS.len();
            let price = 8_500 + (sku as i64 * 14_620) + (m as i64 * 627 + (i as i64 * 53)) % 11_500;
            let items = vec![
                Item {
                    product: sku,
                    variant: (n % 2) as u8,
                    unit: "шт",
                    qty: 1 + (n % 5) as i64,
                    price,
                    supplier: VENDORS[n % 4],
                },
                Item {
                    product: (sku + 2) % PRODUCTS.len(),
                    variant: 0,
                    unit: "компл.",
                    qty: 2,
                    price: price * 2 + (i as i64 * 493),
                    supplier: VENDORS[(n + 1) % 4],
                },
            ];
            let amount = items.iter().map(|x| x.qty * x.price).sum();
            let channel = if owner == "sales" { "direct" } else { "tender" };
            let kind = if channel == "direct" || n % 2 == 1 {
                Kind::Quote
            } else {
                Kind::Tender
            };
            let submitted = accepted.then_some(Moment(date.add(2), 10 * 60));
            let closed = match (kind, submitted) {
                (Kind::Quote, Some(at)) => Some(at),
                (Kind::Tender, Some(_)) => None,
                _ => completed,
            };
            out.push(Doc {
                no: format!(
                    "{}-{:04}",
                    if kind == Kind::Quote { "КП" } else { "ТН" },
                    3200 + n
                ),
                kind,
                owner,
                party: BUYERS[n % 6],
                channel,
                started: born,
                due: planned,
                closed,
                submitted,
                final_state: state,
                amount,
                items: vec![],
                related: parent.clone(),
            });
            out.push(Doc {
                no: format!("Т-{:06}", 670 + n),
                kind: Kind::Supply,
                owner: "supply",
                party: BUYERS[n % 6],
                channel,
                started: born,
                due: planned,
                closed: completed,
                submitted: None,
                final_state: "Отработана",
                amount,
                items,
                related: parent.clone(),
            });
            if i % 2 == 0 {
                out.push(Doc {
                    no: format!("ТР-{:05}", 4100 + n),
                    kind: Kind::Transport,
                    owner: "logistics",
                    party: BUYERS[n % 6],
                    channel,
                    started: born,
                    due: Some(Moment(date.add(1), born.1)),
                    closed: completed,
                    submitted: None,
                    final_state: "Отработана",
                    amount: 18_000 + (n as i64 * 730) % 46_000,
                    items: vec![],
                    related: parent.clone(),
                });
                out.push(Doc {
                    no: format!("ПЭ-{:05}", 1900 + n),
                    kind: Kind::Freight,
                    owner: "logistics",
                    party: CARRIERS[n % 3],
                    channel,
                    started: Moment(date.add(1), 9 * 60),
                    due: Some(Moment(date.add(4), 17 * 60)),
                    closed: completed.map(|d| Moment(d.0.add(1), 15 * 60)),
                    submitted: None,
                    final_state: "Завершено",
                    amount: 18_000 + (n as i64 * 730) % 46_000,
                    items: vec![],
                    related: parent,
                });
            }
        }
    }
    out
}
impl Doc {
    fn finished(&self, asof: Moment) -> Option<Moment> {
        self.closed.filter(|t| *t <= asof)
    }
    fn status(&self, asof: Moment) -> &'static str {
        if self.kind == Kind::Tender
            && self.submitted.is_some_and(|v| v <= asof)
            && self.final_state == "Действует"
        {
            return "Действует";
        }
        if self.finished(asof).is_some() {
            return self.final_state;
        }
        match self.kind {
            Kind::Freight if self.started.0 < asof.0 => "В пути",
            Kind::Freight => "Выдан",
            Kind::Tender | Kind::Quote if self.submitted.is_some_and(|v| v <= asof) => "Действует",
            Kind::Tender | Kind::Quote => "Черновик",
            _ if self.started.0 == asof.0 => "Поступила",
            _ => "В работе",
        }
    }
    fn open(&self, asof: Moment) -> bool {
        if self.kind == Kind::Tender {
            return matches!(
                self.status(asof),
                "Действует" | "Черновик" | "Подготавливается"
            );
        }
        self.finished(asof).is_none()
    }
}

fn previous_price<'a>(data: &'a [Doc], doc: &Doc, item: &Item) -> Option<(&'a Doc, &'a Item)> {
    let limit = doc.closed?;
    let mut found = None;
    let mut newest = None;
    let mut unique = false;
    for earlier in data {
        let Some(closed) = earlier.closed.filter(|v| *v < limit) else {
            continue;
        };
        if earlier.kind != Kind::Supply
            || earlier.final_state != "Отработана"
            || earlier.no == doc.no
        {
            continue;
        }
        for old in &earlier.items {
            if (old.product, old.unit, old.variant) != (item.product, item.unit, item.variant)
                || old.price <= 0
                || old.supplier.is_empty()
            {
                continue;
            }
            if newest.is_none_or(|x| closed > x) {
                newest = Some(closed);
                found = Some((earlier, old));
                unique = true;
            } else if newest == Some(closed) {
                unique = false;
            }
        }
    }
    if unique {
        found
    } else {
        None
    }
}
fn doc_detail(data: &[Doc], doc: &Doc, asof: Moment) -> String {
    let closed = doc.finished(asof);
    let mut html = format!(
        r#"<div class="detail" id="ops-record"><strong>{} · {}</strong><p>{} · {} · {}</p><p>Поступила {} · Срок {} · Завершена {}</p><p>Основание {} · Сумма {}</p>"#,
        doc.kind.label(),
        esc(&doc.no),
        esc(doc.party),
        esc(name(doc.owner)),
        doc.status(asof),
        doc.started.show(),
        doc.due.map(Moment::show).unwrap_or_else(|| "—".into()),
        closed.map(Moment::show).unwrap_or_else(|| "—".into()),
        esc(&doc.related),
        money(doc.amount)
    );
    if doc.kind == Kind::Supply && closed.is_some() {
        html += r#"<h3>Сравнение цен без НДС</h3><div class="table-scroll"><table><thead><tr><th>Номенклатура</th><th>Цена в заявке</th><th>Предыдущая цена</th><th>Изменение</th></tr></thead><tbody>"#;
        for item in &doc.items {
            let old = previous_price(data, doc, item);
            let (prev, change) = match old {
                Some((previous, v)) => {
                    let delta = item.price - v.price;
                    let pct = 100.0 * (delta as f64) / (v.price as f64);
                    let tone = if delta > 0 {
                        "bad"
                    } else if delta < 0 {
                        "good"
                    } else {
                        "muted"
                    };
                    let direction = if delta > 0 {
                        "↑"
                    } else if delta < 0 {
                        "↓"
                    } else {
                        "="
                    };
                    let bias = if pct > 0.0 { "+" } else { "" };
                    (
                        format!(
                            r#"{}/{}<br><small>{} · {}</small>"#,
                            money(v.price),
                            v.unit,
                            esc(&previous.no),
                            previous.closed.unwrap().0.show()
                        ),
                        format!(
                            r#"<span class="{tone}">{direction} {} · {bias}{:.1}%</span>"#,
                            money(delta.abs()),
                            pct
                        ),
                    )
                }
                None => ("—".to_owned(), "—".to_owned()),
            };
            write!(html,r#"<tr><td>{}<br><small>{} · {} {}</small></td><td>{}/{}</td><td>{prev}</td><td>{change}</td></tr>"#,PRODUCTS[item.product],esc(item.supplier),item.qty,item.unit,money(item.price),item.unit).unwrap();
        }
        html += "</tbody></table></div>";
    }
    html += "</div>";
    html
}

fn hits(doc: &Doc, key: &str, r: (Date, Date), asof: Moment) -> bool {
    let done = doc.finished(asof);
    let closed_in =
        done.is_some_and(|v| in_range(v.0, r)) && !doc.open(asof) && doc.final_state != "Отменено";
    let due = doc.due;
    match key {
        "created" => in_range(doc.started.0, r),
        "closed" => closed_in,
        "open" => doc.open(asof),
        "late" => doc.open(asof) && due.is_some_and(|d| d < asof),
        "today" => doc.open(asof) && due.is_some_and(|d| d.0 == asof.0),
        "closed_late" => closed_in && done.zip(due).is_some_and(|(a, b)| a > b),
        "on_time" => closed_in && done.zip(due).is_some_and(|(a, b)| a <= b),
        "unplanned" => closed_in && due.is_none(),
        "submitted" => {
            matches!(doc.kind, Kind::Quote | Kind::Tender)
                && doc.status(asof) == "Действует"
                && doc.submitted.is_some_and(|v| v <= asof && in_range(v.0, r))
        }
        "on_road" => doc.kind == Kind::Freight && doc.status(asof) == "В пути",
        "ship_today" => doc.kind == Kind::Freight && doc.started.0 == asof.0,
        "registry" => doc.kind == Kind::Transport && in_range(doc.started.0, r),
        _ => false,
    }
}

pub(super) fn render(s: &State) -> String {
    let r = metric_range(s);
    let cutoff = r.1.min(SNAPSHOT);
    let asof = Moment(
        cutoff,
        if cutoff == SNAPSHOT {
            12 * 60
        } else {
            23 * 60 + 59
        },
    );
    let logistics = s.route == "metrics-logistics";
    let supply = s.route == "metrics-supply";
    let data = dataset();
    let search = s.search.trim().to_lowercase();
    let channel = value(s, &key_for(s, "channel"), "all");
    let ids: &[&str] = if logistics {
        &["logistics"]
    } else if supply {
        &["supply"]
    } else {
        &SELLERS
    };
    let filtered: Vec<_> = data
        .iter()
        .filter(|d| {
            r.0 <= cutoff
                && d.started <= asof
                && allowed(s, d.owner)
                && ids.contains(&d.owner)
                && (channel == "all" || channel == d.channel)
                && (d.no.to_lowercase().contains(&search)
                    || d.party.to_lowercase().contains(&search)
                    || d.related.to_lowercase().contains(&search))
        })
        .collect();
    let mut html = title(
        "Метрики · Битрикс24 и 1С",
        if logistics {
            "Логистика"
        } else if supply {
            "Снабжение"
        } else {
            "День менеджера"
        },
    );
    write!(html,r#"<section class="panel toolbar"><label>С <input type="date" data-act="ops-from" value="{}"></label><label>По <input type="date" data-act="ops-to" value="{}"></label>{}<label>Канал <select data-act="ops-channel">"#,r.0.iso(),r.1.iso(),people_select(s,ids)).unwrap();
    for (id, label) in [
        ("all", "Все"),
        ("direct", "Прямые продажи"),
        ("tender", "Тендеры"),
    ] {
        write!(
            html,
            r#"<option value="{id}" {}>{label}</option>"#,
            if channel == id { "selected" } else { "" }
        )
        .unwrap();
    }
    write!(html,r#"</select></label><label>Поиск <input type="search" data-act="ops-search" value="{}" placeholder="Номер или контрагент"></label></section><div class="stat-grid">"#,esc(&s.search)).unwrap();
    let mut tiles = vec![
        ("created", "Созданы"),
        ("closed", "Закрыты"),
        ("open", "В работе"),
        ("late", "Просрочены"),
    ];
    if supply {
        tiles.extend([
            ("today", "Срок сегодня"),
            ("closed_late", "Закрыты с опозданием"),
            ("unplanned", "Закрыты без плана"),
            ("on_time", "В срок"),
        ]);
    } else if logistics {
        tiles.extend([
            ("on_road", "В пути сегодня"),
            ("ship_today", "К отгрузке сегодня"),
            ("registry", "Заявки на расчёт ТР"),
        ]);
    } else {
        tiles.extend([("submitted", "Поданы КП и тендеры"), ("on_time", "В срок")]);
    }
    let selected = drill(s);
    for &(key, label) in &tiles {
        let count = filtered.iter().filter(|d| hits(d, key, r, asof)).count();
        let display = if key == "on_time" {
            let known = count
                + filtered
                    .iter()
                    .filter(|d| hits(d, "closed_late", r, asof))
                    .count();
            if known == 0 {
                "—".into()
            } else {
                format!("{:.0}%", 100.0 * count as f64 / known as f64)
            }
        } else {
            count.to_string()
        };
        html += &tile(key, label, &display, key == selected);
    }
    html += "</div>";
    let default = if logistics { "registry" } else { "open" };
    let key = if tiles.iter().any(|(k, _)| *k == selected) {
        selected
    } else {
        default
    };
    let heading = if selected.is_empty() && logistics {
        "Заявки в отдел логистики — реестр"
    } else if selected.is_empty() {
        "Очередь на дату среза"
    } else {
        tiles
            .iter()
            .find(|(k, _)| *k == key)
            .map(|t| t.1)
            .unwrap_or("Реестр")
    };
    let mut picked = filtered
        .into_iter()
        .filter(|d| hits(d, key, r, asof))
        .collect::<Vec<_>>();
    picked.sort_by_key(|d| std::cmp::Reverse(d.started));
    write!(html,r#"<section class="panel" id="ops-detail"><div class="panel-head"><h2>{heading}</h2><span class="chip">{}</span></div><div class="table-scroll"><table><thead><tr><th>Документ</th><th>Контрагент</th><th>Состояние</th><th>Срок</th><th>Завершён</th></tr></thead><tbody>"#,picked.len()).unwrap();
    if picked.is_empty() {
        html += &empty(5)
    }
    for doc in picked {
        let late = hits(doc, "late", r, asof) || hits(doc, "closed_late", r, asof);
        let pair = doc.kind == Kind::Supply
            && doc.finished(asof).is_some()
            && doc
                .items
                .iter()
                .any(|i| previous_price(&data, doc, i).is_some());
        write!(html,r#"<tr><td>{} {}<br><small>{}</small></td><td>{}</td><td class="{}">{}</td><td>{}</td><td>{}</td></tr>"#,button("ops-doc",&doc.no,&doc.no,s.selected==doc.no),
            if pair{button("ops-doc",&doc.no,"Есть сравнение цен",s.selected==doc.no)}else{String::new()},doc.kind.label(),esc(doc.party),if late{"bad"}else{""},doc.status(asof),doc.due.map(Moment::show).unwrap_or_else(||"—".into()),doc.finished(asof).map(Moment::show).unwrap_or_else(||"—".into())).unwrap();
        if s.selected == doc.no {
            write!(
                html,
                r#"<tr><td colspan="5">{}</td></tr>"#,
                doc_detail(&data, doc, asof)
            )
            .unwrap();
        }
    }
    html += "</tbody></table></div></section>";
    html
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn price_uses_last_completed_matching_unit_and_variant_only() {
        let data = dataset();
        let latest = data
            .iter()
            .rev()
            .find(|d| d.kind == Kind::Supply && d.closed.is_some())
            .unwrap();
        let item = &latest.items[0];
        let (past, old) = previous_price(&data, latest, item).unwrap();
        assert!(past.closed.unwrap() < latest.closed.unwrap());
        assert_eq!(item.variant, old.variant);
        let mut foreign = item.clone();
        foreign.unit = "кг";
        assert!(previous_price(&data, latest, &foreign).is_none());
    }
    #[test]
    fn submitted_uses_active_status_and_transition_date() {
        let data = dataset();
        let period = (Date(2026, 11, 1), SNAPSHOT);
        let asof = Moment(SNAPSHOT, 720);
        let submitted: Vec<_> = data
            .iter()
            .filter(|d| hits(d, "submitted", period, asof))
            .collect();
        assert!(!submitted.is_empty());
        assert!(submitted
            .iter()
            .all(|d| d.status(asof) == "Действует" && in_range(d.submitted.unwrap().0, period)));
    }
}
