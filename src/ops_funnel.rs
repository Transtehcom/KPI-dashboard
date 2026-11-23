use super::*;
use crate::money;
use std::fmt::Write;

#[derive(Copy, Clone, PartialEq, Eq)]
enum Entity {
    Lead,
    Deal,
}
#[derive(Copy, Clone, PartialEq, Eq)]
enum Stage {
    New,
    Working,
    Quoted,
    Contract,
    Won,
    Lost,
}
impl Stage {
    fn label(self, kind: Entity) -> &'static str {
        match (self, kind) {
            (Self::New, Entity::Lead) => "Распределено",
            (Self::New, _) => "Новая сделка",
            (Self::Working, _) => "В работе",
            (Self::Quoted, Entity::Lead) => "Обработано",
            (Self::Quoted, _) => "Преддоговорная",
            (Self::Contract, _) => "Исполнение договора",
            (Self::Won, _) => "Закрыто успешно",
            (Self::Lost, _) => "Закрыто неуспешно",
        }
    }
    fn open(self) -> bool {
        !matches!(self, Self::Won | Self::Lost)
    }
}
struct Entry {
    id: String,
    kind: Entity,
    owner: &'static str,
    buyer: &'static str,
    product: &'static str,
    amount: i64,
    events: Vec<(Date, Stage)>,
}
fn records() -> Vec<Entry> {
    let mut data = Vec::new();
    for m in 1..=11 {
        for i in 0..21_usize {
            let n = (m as usize - 1) * 21 + i;
            let start = Date(2026, m, 1 + ((i * 5 + m as usize) % 27) as u32);
            if start > SNAPSHOT {
                continue;
            }
            let kind = if n % 2 == 0 {
                Entity::Lead
            } else {
                Entity::Deal
            };
            let mut events = vec![(start, Stage::New)];
            if i % 7 != 1 {
                events.push((start.add(1 + (i % 4) as u32), Stage::Working));
            }
            if i % 5 != 2 && i % 7 != 1 {
                events.push((start.add(6), Stage::Quoted));
            }
            if kind == Entity::Deal && i % 3 != 1 {
                events.push((start.add(10), Stage::Contract));
            }
            if i % 4 == 0 {
                events.push((start.add(13), Stage::Won));
            }
            if i % 6 == 3 {
                events.push((start.add(16), Stage::Lost));
            }
            events.retain(|(at, _)| *at <= SNAPSHOT);
            data.push(Entry {
                id: format!(
                    "{}-{:04}",
                    if kind == Entity::Deal { "СД" } else { "ЛД" },
                    n + 1001
                ),
                kind,
                owner: SELLERS[n % SELLERS.len()],
                buyer: BUYERS[(i + m as usize) % BUYERS.len()],
                product: PRODUCTS[n % PRODUCTS.len()],
                amount: if kind == Entity::Deal {
                    (145_000 + (n as i64 * 34_123) % 1_995_000) * ((i % 4 + 1) as i64)
                } else {
                    0
                },
                events,
            });
        }
    }
    data
}
#[derive(Copy, Clone)]
enum Group {
    Created,
    Open,
    Stage(Stage),
}
impl Group {
    fn matches(self, st: Stage) -> bool {
        match self {
            Self::Open => st.open(),
            Self::Stage(target) => st == target,
            Self::Created => false,
        }
    }
    fn select(self, e: &Entry, fact: bool, field: &str, r: (Date, Date)) -> Option<(Date, Stage)> {
        let first = e.events[0];
        let last = *e.events.last()?;
        if fact {
            if matches!(self, Self::Created) {
                return in_range(first.0, r).then_some(first);
            }
            return e
                .events
                .iter()
                .rev()
                .copied()
                .find(|(at, st)| in_range(*at, r) && self.matches(*st));
        }
        let stamp = if field == "stage" { last.0 } else { first.0 };
        (last.1.open() && in_range(stamp, r) && self.matches(last.1)).then_some(last)
    }
}

pub(super) fn render(s: &State) -> String {
    let fact = s.route == "funnel-fact";
    let field = value(s, "ops:date-field", "created");
    let channel = value(s, &key_for(s, "channel"), "all");
    let mut html = title(
        "Битрикс24 · Воронки",
        if fact {
            "Воронки · факт"
        } else {
            "Воронки по дате"
        },
    );
    html += r#"<section class="panel toolbar">"#;
    html += &people_select(s, &SELLERS);
    html += r#"<label>Канал <select data-act="ops-channel">"#;
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
    html += "</select></label>";
    if !fact {
        html += &button(
            "ops-date-field",
            "created",
            "По дате создания",
            field == "created",
        );
        html += &button(
            "ops-date-field",
            "stage",
            "По дате смены стадии",
            field == "stage",
        );
    }
    html += "</section>";
    let data = records();
    let rows = data
        .iter()
        .filter(|e| {
            allowed(s, e.owner)
                && (channel == "all" || (channel == "direct") == (e.owner == "sales"))
        })
        .collect::<Vec<_>>();
    let r = range(s);
    let mut details = String::new();
    for (prefix, kind, label) in [
        ("lead", Entity::Lead, "Лиды"),
        ("deal", Entity::Deal, "Сделки"),
    ] {
        let mut groups = Vec::new();
        if fact {
            groups.push(("created", "Создано в периоде", Group::Created));
        }
        if kind == Entity::Lead {
            groups.extend([
                ("new", "Распределено", Group::Stage(Stage::New)),
                ("work", "В работе", Group::Stage(Stage::Working)),
                ("processed", "Обработано", Group::Stage(Stage::Quoted)),
            ]);
        } else {
            groups.extend([
                ("all", "В работе", Group::Open),
                (
                    "precontract",
                    "Преддоговорные сделки",
                    Group::Stage(Stage::Quoted),
                ),
                (
                    "contract",
                    "Договорные сделки",
                    Group::Stage(Stage::Contract),
                ),
            ]);
        }
        if fact {
            groups.extend([
                ("won", "Закрыто успешно", Group::Stage(Stage::Won)),
                ("lost", "Закрыто неуспешно", Group::Stage(Stage::Lost)),
            ]);
        }
        let mut matched = std::collections::BTreeSet::new();
        let mut bars = String::new();
        let mut stages = Vec::new();
        for (group, title, scope) in groups {
            let items: Vec<_> = rows
                .iter()
                .copied()
                .filter(|e| e.kind == kind)
                .filter_map(|e| scope.select(e, fact, field, r).map(|at| (e, at)))
                .collect();
            for (e, _) in &items {
                matched.insert(e.id.as_str());
            }
            stages.push((group, title, items));
        }
        for (group, title, items) in stages {
            let key = format!("{prefix}-{group}");
            let active = s.selected == key;
            let pct = items.len() * 100 / matched.len().max(1);
            let amount = items.iter().map(|(e, _)| e.amount).sum();
            write!(bars,r#"<tr><td>{}</td><td><meter min="0" max="100" value="{pct}" aria-label="Доля записей">{pct}%</meter> {pct}%</td><td>{}</td>{}</tr>"#,
                button("ops-stage",&key,title,active),items.len(),if kind==Entity::Deal{format!("<td>{}</td>",money(amount))}else{String::new()}).unwrap();
            if active {
                write!(details,r#"<section class="panel detail" id="ops-detail"><div class="panel-head"><h2>{label} · {title}</h2><span>{} записей</span></div><div class="table-scroll"><table><thead><tr><th>Карточка</th><th>Ответственный</th><th>Стадия</th><th>Переход</th>{}</tr></thead><tbody>"#,items.len(),if kind==Entity::Deal{"<th>Сумма</th>"}else{""}).unwrap();
                if items.is_empty() {
                    details += &empty(5);
                }
                for (entry, (at, stage)) in &items {
                    write!(
                        details,
                        "<tr><td><details><summary>{} · {}</summary><p>{}</p><ul>",
                        entry.id,
                        esc(entry.buyer),
                        entry.product
                    )
                    .unwrap();
                    for (date, st) in &entry.events {
                        if *date <= *at {
                            write!(details, "<li>{} · {}</li>", date.show(), st.label(kind))
                                .unwrap();
                        }
                    }
                    write!(
                        details,
                        "</ul></details></td><td>{}</td><td>{}</td><td>{}</td>{}</tr>",
                        esc(name(entry.owner)),
                        stage.label(kind),
                        at.show(),
                        if kind == Entity::Deal {
                            format!("<td>{}</td>", money(entry.amount))
                        } else {
                            String::new()
                        }
                    )
                    .unwrap();
                }
                details += "</tbody></table></div></section>";
            }
        }
        write!(html,r#"<section class="panel"><div class="panel-head"><h2>{label}</h2><span class="chip">{}</span></div><div class="table-scroll"><table><thead><tr><th>Этап</th><th>Доля воронки</th><th>Количество</th>{}</tr></thead><tbody>{bars}</tbody></table></div></section>"#,matched.len(),if kind==Entity::Deal{"<th>Сумма</th>"}else{""}).unwrap();
    }
    html += &details;
    html
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn historical_moves_and_current_open_snapshot_differ() {
        let e = Entry {
            id: "1".into(),
            kind: Entity::Deal,
            owner: "sales",
            buyer: "",
            product: "",
            amount: 10,
            events: vec![
                (Date(2026, 9, 29), Stage::New),
                (Date(2026, 10, 2), Stage::Quoted),
                (Date(2026, 10, 3), Stage::Quoted),
                (Date(2026, 10, 6), Stage::Won),
            ],
        };
        let r = (Date(2026, 10, 1), Date(2026, 10, 31));
        assert!(Group::Open.select(&e, false, "created", r).is_none());
        assert_eq!(
            Group::Stage(Stage::Quoted)
                .select(&e, true, "stage", r)
                .unwrap()
                .0,
            Date(2026, 10, 3)
        );
        assert!(Group::Created.select(&e, true, "created", r).is_none());
    }
}
