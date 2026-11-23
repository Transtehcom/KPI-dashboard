use std::collections::BTreeMap;
use std::sync::OnceLock;

const LEGAL: &[&str] = &[
    "ООО", "АО", "ПАО", "НАО", "ОАО", "ЗАО", "ИП", "ГУП", "МУП", "ФГУП", "ФГБУ", "ГБУ", "МБУ",
    "ФКУ", "ГКУ", "АНО", "НКО", "НП", "ПТ", "КФХ", "СНТ",
];
const GROUPS: &[&str] = &[
    "ГК", "ТК", "ТД", "ТПК", "ПКФ", "ПТК", "НПК", "НПП", "НПО", "НТЦ", "СК", "ПФ", "АКБ",
];
const FULL: &[(&str, &str)] = &[
    ("общество с ограниченной ответственностью", "ООО"),
    ("публичное акционерное общество", "ПАО"),
    ("открытое акционерное общество", "ОАО"),
    ("закрытое акционерное общество", "ЗАО"),
    ("акционерное общество", "АО"),
    ("индивидуальный предприниматель", "ИП"),
    ("группа компаний", "ГК"),
];
fn word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// Unicode default case folding for the CRM search contract (including ß and final sigma).
pub(super) fn fold(text: &str) -> String {
    static SPECIAL: OnceLock<BTreeMap<char, String>> = OnceLock::new();
    let special = SPECIAL.get_or_init(|| {
        serde_json::from_str(include_str!("casefold.json"))
            .expect("generated Unicode case-fold table")
    });
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        if let Some(mapped) = special.get(&c) {
            out.push_str(mapped)
        } else {
            out.extend(c.to_lowercase())
        }
    }
    out
}
pub(super) fn company(input: &str) -> String {
    let text = input.split_whitespace().collect::<Vec<_>>().join(" ");
    let mut tokens: Vec<String> = Vec::new();
    for c in text.chars() {
        if word(c)
            && tokens
                .last()
                .is_some_and(|w| w.chars().next().is_some_and(word))
        {
            tokens.last_mut().unwrap().push(c);
        } else {
            tokens.push(c.to_string());
        }
    }
    for &(phrase, abbr) in FULL {
        let parts = phrase.split(' ').collect::<Vec<_>>();
        let len = parts.len() * 2 - 1;
        let mut i = 0;
        while i + len <= tokens.len() {
            let matched = parts.iter().enumerate().all(|(n, w)| {
                tokens[i + n * 2].to_lowercase() == *w && (n == 0 || tokens[i + n * 2 - 1] == " ")
            });
            if matched {
                tokens.splice(i..i + len, [abbr.to_string()]);
            }
            i += 1;
        }
    }
    let expanded = tokens.concat();
    let mut found = Vec::new();
    let mut name = String::new();
    for token in tokens {
        let upper = token.to_uppercase();
        if LEGAL.contains(&upper.as_str()) || GROUPS.contains(&upper.as_str()) {
            if !found.contains(&upper) {
                found.push(upper);
            }
        } else {
            name.push_str(&token);
        }
    }
    for (left, right) in [('(', ')'), ('[', ']')] {
        let mut chars = name.chars().peekable();
        let mut cleaned = String::new();
        while let Some(c) = chars.next() {
            if c == left {
                let mut spaces = String::new();
                while chars.peek().is_some_and(|c| c.is_whitespace()) {
                    spaces.push(chars.next().unwrap());
                }
                if chars.next_if_eq(&right).is_some() {
                    continue;
                }
                cleaned.push(c);
                cleaned.push_str(&spaces);
            } else {
                cleaned.push(c);
            }
        }
        name = cleaned;
    }
    let normalized = name.split_whitespace().collect::<Vec<_>>().join(" ");
    let mut bare = normalized.trim_matches(|c| " ,;-–—".contains(c));
    for (opening, closing) in [('"', '"'), ('\'', '\''), ('«', '»'), ('„', '“'), ('“', '”')]
    {
        let expected = if opening == closing { 2 } else { 1 };
        if bare.starts_with(opening)
            && bare.ends_with(closing)
            && bare.chars().count() > 1
            && bare.matches(opening).count() == expected
            && bare.matches(closing).count() == expected
        {
            bare = bare[opening.len_utf8()..bare.len() - closing.len_utf8()].trim();
        } else if bare.starts_with(opening)
            && bare.matches(opening).count() == 1
            && (opening == closing || !bare.contains(closing))
        {
            bare = bare[opening.len_utf8()..].trim();
        }
    }
    if bare.is_empty() {
        return expanded;
    }
    if found.is_empty() {
        return bare.to_owned();
    }
    let ordered = found
        .iter()
        .filter(|a| GROUPS.contains(&a.as_str()))
        .chain(found.iter().filter(|a| LEGAL.contains(&a.as_str())))
        .map(String::as_str)
        .collect::<Vec<_>>();
    format!("{bare}, {}", ordered.join(" "))
}
fn region_key(text: &str) -> String {
    const STOP: &[&str] = &[
        "республика",
        "респ",
        "область",
        "обл",
        "край",
        "город",
        "г",
        "россия",
        "российская",
        "федерация",
        "рф",
        "автономный",
        "автономная",
        "авт",
        "округ",
        "ао",
    ];
    text.to_lowercase()
        .replace('ё', "е")
        .split(|c: char| !(c.is_ascii_lowercase() || ('а'..='я').contains(&c)))
        .filter(|w| !STOP.contains(w))
        .collect()
}
struct Regions {
    names: BTreeMap<String, String>,
    aliases: BTreeMap<String, String>,
}
fn regions() -> &'static Regions {
    static REG: OnceLock<Regions> = OnceLock::new();
    REG.get_or_init(|| {
        let names: BTreeMap<String, String> =
            serde_json::from_str(include_str!("regions.json")).expect("region catalog");
        let mut aliases: BTreeMap<String, String> = names
            .iter()
            .map(|(c, n)| (region_key(n), c.clone()))
            .collect();
        for (code, words) in [
            ("14", "Саха|Якутия"),
            ("15", "Северная Осетия|Алания"),
            ("13", "Саранск"),
            (
                "16",
                "Татарстан|Татарстан (Татарстан)|Казань|Набережные Челны",
            ),
            ("21", "Чувашия|Чувашская|Чувашская Республика - Чувашия"),
            ("42", "Кемеровская область|Кузбасс"),
            ("54", "Новосибирск"),
            ("59", "Пермь"),
            ("66", "Екатеринбург"),
            ("86", "Ханты-Мансийский округ|ХМАО|Югра"),
            ("72", "Тюмень"),
            ("74", "Челябинскаяобласть"),
            ("78", "С-Петербург|Санкт-Петербург Финляндский округ"),
            ("89", "ЯНАО"),
        ] {
            for v in words.split('|') {
                aliases.insert(region_key(v), code.to_owned());
            }
        }
        Regions { names, aliases }
    })
}
pub(super) fn region(text: &str) -> Option<String> {
    let reg = regions();
    let mut names = Vec::new();
    for region in text
        .trim()
        .split(';')
        .map(str::trim)
        .filter(|p| !p.is_empty())
    {
        let code = if (1..=2).contains(&region.len()) && region.bytes().all(|b| b.is_ascii_digit())
        {
            format!("{region:0>2}")
        } else {
            reg.aliases.get(&region_key(region))?.clone()
        };
        let name = format!("{}-{}", code, reg.names.get(&code)?);
        if !names.contains(&name) {
            names.push(name);
        }
    }
    (!names.is_empty()).then(|| names.join("; "))
}
