use crate::{employees, kpi, money, permitted, title, viewer, State, PAGES};

pub fn who(s: &State) -> String {
    if s.role == "admin" {
        s.values
            .get("home-person")
            .cloned()
            .unwrap_or_else(|| "all".into())
    } else {
        viewer(s).into()
    }
}
const ICONS: [&str; 10] = [
    r#"<path d="M3 20h18M6 16V9m6 7V4m6 12v-5"/>"#,
    r#"<path d="M3 4h18l-7 8v6l-4 2v-8z"/>"#,
    r#"<path d="M6 3l3 5-2 2c1 3 4 6 7 7l2-2 5 3-2 3C11 21 3 13 3 5z"/>"#,
    r#"<path d="M3 5h18v13H9l-5 3v-3H3zM7 10h1m3 0h1m3 0h1"/>"#,
    r#"<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M8 3v4m8-4v4M3 10h18M7 15h5"/>"#,
    r#"<path d="M3 3v17h18M6 15l4-5 4 3 6-7"/>"#,
    r#"<rect x="3" y="5" width="18" height="15" rx="2"/><path d="M3 10h18m-6 6h3"/>"#,
    r#"<path d="M2 6h12v11H2zM14 10h4l4 4v3h-8"/><circle cx="6" cy="18" r="2"/><circle cx="18" cy="18" r="2"/>"#,
    r#"<path d="M3 7l9-4 9 4-9 5zM3 7v10l9 4 9-4V7m-9 5v9"/>"#,
    r#"<circle cx="9" cy="8" r="3"/><path d="M2 20v-2a7 7 0 0114 0v2m0-15a3 3 0 010 6m2 3a6 6 0 014 6"/>"#,
];
fn icon(i: usize) -> String {
    format!(
        r#"<svg class="tile-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{}</svg>"#,
        ICONS[i]
    )
}
fn link(path: &str, text: &str) -> String {
    format!(r##"<a href="#{path}" data-act="nav" data-val="{path}">{text}</a>"##)
}
fn cards(s: &State) -> String {
    PAGES.iter().enumerate().filter(|(_,p)|permitted(s,p.path)).map(|(i,p)|{
        let label=title(s,p);
        let desc=if p.path=="kpi" && s.role!="admin" {"Мои показатели, планы и премия"}else{p.desc};
        let front=format!(r#"<span class="tile-top">{}<strong>{label}</strong></span><span class="tile-desc">{desc}</span>"#,icon(i));
        if p.children.is_empty(){
            format!(r##"<a class="tile" href="#{path}" data-act="nav" data-val="{path}">{front}</a>"##,path=p.path)
        }else{
            let links=p.children.iter().map(|(path,name)|link(path,name)).collect::<String>();
            format!(r#"<div class="tile split"><button class="tile-front" type="button" data-split aria-expanded="false" aria-label="{label} — выбрать страницу">{front}</button><div class="tile-choices">{links}</div></div>"#)
        }
    }).collect()
}
fn ring(counts: &[usize], labels: &[&str], colors: &[&str], filters: &[&str]) -> String {
    let total: usize = counts.iter().sum();
    let mut pos = 0.;
    let mut svg = String::new();
    let mut legend = String::new();
    for (i, &count) in counts.iter().enumerate() {
        let pct = if total == 0 {
            0.
        } else {
            100. * count as f64 / total as f64
        };
        let color = colors[i];
        let key = filters[i];
        let label = labels[i];
        if count > 0 {
            svg.push_str(&format!(r#"<circle cx="50" cy="50" r="39" pathLength="100" fill="none" stroke="{color}" stroke-width="13" stroke-dasharray="{pct:.6} {:.6}" stroke-dashoffset="{:.6}" data-act="home-work" data-val="{key}" tabindex="0" role="button" aria-label="{label} · {count}"><title>{label} · {count}</title></circle>"#,100.-pct,-pos));
        }
        legend.push_str(&format!(r#"<button data-act="home-work" data-val="{key}"><i style="--tone:{color}"></i><span>{label}</span><b>{count}</b></button>"#));
        pos += pct;
    }
    format!(
        r#"<div class="ring-row"><div class="ring"><svg viewBox="0 0 100 100"><circle cx="50" cy="50" r="39" fill="none" stroke="var(--line)" stroke-width="13"/>{svg}</svg><span><b>{total}</b><small>всего</small></span></div><div class="ring-legend">{legend}</div></div>"#
    )
}
fn payout(s: &State, id: &str) -> Option<i64> {
    if id == "all" {
        let results: Vec<_> = employees()
            .iter()
            .filter_map(|e| kpi::monthly_payout(s, e.0))
            .collect();
        (!results.is_empty()).then(|| results.iter().sum())
    } else {
        kpi::monthly_payout(s, id)
    }
}
fn chart(s: &State, id: &str) -> String {
    let num: u32 = s.period.get(5..).and_then(|v| v.parse().ok()).unwrap_or(11);
    let names = [
        "янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек",
    ];
    let vals: Vec<_> = (num.saturating_sub(5).max(1)..=num)
        .map(|m| {
            let mut previous = s.clone();
            previous.period = format!("2026-{m:02}");
            (m, payout(&previous, id).unwrap_or_default())
        })
        .collect();
    let max = vals.iter().map(|v| v.1).max().unwrap_or(1).max(1);
    vals.iter().map(|(m,n)| format!(r#"<button style="--height:{:.1}%" data-act="home-pay" data-val="2026-{m:02}" title="{} · {}" aria-label="{} · {}"><i></i><span>{}</span></button>"#,(*n as f64/max as f64*100.).max(2.),names[*m as usize-1],money(*n),names[*m as usize-1],money(*n),names[*m as usize-1])).collect()
}
pub fn render(s: &State) -> String {
    let minute = s
        .values
        .get("local-minute")
        .and_then(|v| v.parse::<i32>().ok())
        .unwrap_or(750);
    let greet = match minute {
        1..=360 => "Доброй ночи",
        361..=720 => "Доброе утро",
        721..=1080 => "Добрый день",
        _ => "Добрый вечер",
    };
    let person = who(s);
    let (_, name, _) = employees().into_iter().find(|e| e.0 == viewer(s)).unwrap();
    let options = std::iter::once(("all", "Вся команда", ""))
        .chain(employees())
        .map(|(id, n, _)| {
            format!(
                r#"<option value="{id}" {}>{n}</option>"#,
                if id == person { "selected" } else { "" }
            )
        })
        .collect::<String>();
    let picker = if s.role == "admin" {
        format!(
            r#"<select data-act="home-person" aria-label="Сотрудник для статистики">{options}</select>"#
        )
    } else {
        String::new()
    };
    let mut stats = String::new();
    if permitted(s, "kpi") {
        let summary = payout(s, &person)
            .map(money)
            .unwrap_or_else(|| "KPI не назначен".into());
        stats.push_str(&format!(r#"<section class="summary-pay"><div class="eyebrow">Начислено по KPI</div><b>{summary}</b><div class="mini-bars">{}</div></section>"#,chart(s,&person)));
    }
    if permitted(s, "workload") {
        let counts = if person == "all" {
            employees()
                .iter()
                .map(|e| kpi::workload_counts(s, e.0))
                .fold([0; 4], |mut total, row| {
                    for i in 0..4 {
                        total[i] += row[i]
                    }
                    total
                })
        } else {
            kpi::workload_counts(s, &person)
        };
        let efficiency = ring(
            &counts,
            &[
                "Закрыты без просрочки",
                "Сданы с опозданием",
                "В работе без просрочки",
                "Открытая просрочка",
            ],
            &["#168b70", "#b77f2a", "#5478bb", "#cf574e"],
            &["done", "done-late", "open", "open-late"],
        );
        let load = ring(
            &[counts[0] + counts[1], counts[2], counts[3]],
            &["Закрыто", "В работе", "Просрочено"],
            &["#168b70", "#5478bb", "#cf574e"],
            &["closed", "open", "open-late"],
        );
        stats.push_str(&format!(r#"<section class="home-stat"><h3>Эффективность</h3>{efficiency}</section><section class="home-stat"><h3>Загрузка</h3>{load}</section>"#));
    }
    let first = name.split_whitespace().next().unwrap_or(name);
    format!(
        r#"<div class="home-heading"><div><div class="eyebrow">Главное меню</div><h1>{greet}, {first}</h1></div></div><div class="home-layout"><nav class="tiles" aria-label="Разделы дашборда">{}</nav><aside class="panel home-stats"><div class="panel-head"><h2>Статистика</h2>{picker}</div>{stats}</aside></div>"#,
        cards(s)
    )
}
