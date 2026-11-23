//! Read-side contracts over a synchronized database snapshot. Data acquisition and
//! authentication are separate: the caller resolves the session on the server.
#[path = "http_contract/format.rs"]
mod format;
use crate::{
    permitted,
    sections::{supplier_columns, xlsx},
    State,
};
use chrono::{Duration, NaiveDateTime};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, HashMap};
use std::sync::OnceLock;

pub struct HttpResponse {
    pub status: u16,
    pub content_type: &'static str,
    pub filename: Option<&'static str>,
    pub body: Vec<u8>,
}
impl HttpResponse {
    fn json(status: u16, data: Value) -> Self {
        Self {
            status,
            content_type: "application/json; charset=utf-8",
            filename: None,
            body: serde_json::to_vec(&data).expect("serializable response"),
        }
    }
    fn excel(filename: &'static str, body: Vec<u8>) -> Self {
        Self {
            status: 200,
            content_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename: Some(filename),
            body,
        }
    }
}

pub const DISPLAY: [&str; 8] = [
    "Название",
    "Реквизит: ИНН",
    "Рабочий телефон",
    "Мобильный телефон",
    "Рабочий e-mail",
    "Корпоративный сайт",
    "Регион",
    "Теги отдела снабжения",
];
pub const CARRIER_COLUMNS: [&str; 11] = [
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
type Fields = BTreeMap<String, String>;

#[derive(Clone, Deserialize, Serialize)]
pub struct Supplier {
    pub id: String,
    pub fields: Fields,
}
impl Supplier {
    pub fn get(&self, field: &str) -> &str {
        self.fields.get(field).map(String::as_str).unwrap_or("")
    }
    fn key(&self) -> String {
        let inn = self.get("Реквизит: ИНН").trim();
        if (inn.len() == 10 || inn.len() == 12) && inn.bytes().all(|b| b.is_ascii_digit()) {
            format!("inn:{inn}")
        } else {
            format!("ref:{}", self.id)
        }
    }
}
#[derive(Clone, Deserialize)]
pub struct Forwarder {
    pub id: String,
    pub responsible: Option<String>,
    #[serde(default)]
    pub contract_files: Vec<String>,
    pub carrier: Option<String>,
    pub carrier_key: Option<String>,
    pub dt: Option<String>,
    pub created_at: Option<String>,
    pub doc: Option<String>,
    pub status: Option<String>,
    pub route: Option<String>,
    pub deleted: Option<i64>,
    #[serde(default)]
    pub carrier_card: BTreeMap<String, Value>,
}
#[derive(Clone, Deserialize, Serialize)]
pub struct Meta {
    pub synced_at: Option<String>,
    pub error: Option<String>,
}
#[derive(Clone, Deserialize)]
pub struct Enrichment {
    pub supplier_key: String,
    pub region: String,
    pub checked_at: String,
}
#[derive(Clone, Deserialize)]
pub struct WeeklyScore {
    pub emp: String,
    pub week: String,
    pub criterion: String,
    pub score: i64,
}
#[derive(Clone, Deserialize)]
pub struct DirectoryData {
    pub suppliers: Vec<Supplier>,
    #[serde(default)]
    pub weekly_scores: Vec<WeeklyScore>,
    pub forwarders: Vec<Forwarder>,
    pub enrichment: Vec<Enrichment>,
    pub meta: Meta,
    #[serde(deserialize_with = "snapshot_time")]
    pub snapshot_at: NaiveDateTime,
    #[serde(default)]
    pub refreshing: bool,
}
fn snapshot_time<'de, D: serde::Deserializer<'de>>(
    deserializer: D,
) -> Result<NaiveDateTime, D::Error> {
    let value = String::deserialize(deserializer)?;
    value.parse().map_err(serde::de::Error::custom)
}
pub fn fixture() -> &'static DirectoryData {
    static DATA: OnceLock<DirectoryData> = OnceLock::new();
    DATA.get_or_init(|| {
        serde_json::from_str(include_str!("http_contract/directory-fixture.json"))
            .expect("synthetic database fixture")
    })
}
pub fn suppliers(data: &DirectoryData, search: &str, as_of: NaiveDateTime) -> Vec<Supplier> {
    let search = format::fold(search).replace('ё', "е");
    let terms = search.split_whitespace().collect::<Vec<_>>();
    let cutoff = (as_of - Duration::days(30))
        .format("%Y-%m-%dT%H:%M:%S")
        .to_string();
    let enrichments = data
        .enrichment
        .iter()
        .filter(|e| e.checked_at >= cutoff)
        .map(|e| (&*e.supplier_key, &*e.region))
        .collect::<HashMap<_, _>>();
    let mut matches = Vec::new();
    for raw in &data.suppliers {
        let mut row = raw.clone();
        row.fields
            .insert("Название".into(), format::company(raw.get("Название")));
        let raw_region = raw.get("Регион");
        let region = format::region(raw_region)
            .or_else(|| enrichments.get(row.key().as_str()).map(|v| v.to_string()))
            .unwrap_or_else(|| raw_region.trim().into());
        row.fields.insert("Регион".into(), region);
        let haystack = format::fold(
            &row.fields
                .values()
                .map(String::as_str)
                .collect::<Vec<_>>()
                .join(" "),
        )
        .replace('ё', "е");
        if terms.iter().all(|word| haystack.contains(word)) {
            matches.push(row);
        }
    }
    matches.sort_by_cached_key(|r| (format::fold(r.get("Название")), r.id.clone()));
    matches
}
pub struct Carrier<'a> {
    pub key: String,
    pub cells: Vec<Value>,
    pub docs: Vec<&'a Forwarder>,
}
pub fn cell(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::String(s) => s.clone(),
        _ => value.to_string(),
    }
}
pub fn carriers<'a>(data: &'a DirectoryData, search: &str) -> Vec<Carrier<'a>> {
    let mut records = data
        .forwarders
        .iter()
        .filter(|d| {
            d.deleted.unwrap_or(0) == 0
                && !d
                    .carrier
                    .as_deref()
                    .unwrap_or("")
                    .trim_matches(' ')
                    .is_empty()
        })
        .collect::<Vec<_>>();
    records.sort_by(|a, b| {
        (&b.dt, &b.created_at, &b.doc, &b.id).cmp(&(&a.dt, &a.created_at, &a.doc, &a.id))
    });
    let mut indices = HashMap::new();
    let mut rows: Vec<Carrier> = Vec::new();
    for doc in records {
        let name = doc.carrier.as_deref().unwrap_or("").trim_matches(' ');
        let key = doc
            .carrier_key
            .clone()
            .filter(|v| !v.is_empty())
            .unwrap_or_else(|| format::fold(name));
        let idx = *indices.entry(key.clone()).or_insert_with(|| {
            let mut cells = vec![
                json!(name),
                json!(0),
                json!(doc.doc),
                json!(doc.dt),
                json!(doc.route),
            ];
            cells.extend(
                CARRIER_COLUMNS[5..]
                    .iter()
                    .map(|f| doc.carrier_card.get(*f).cloned().unwrap_or(Value::Null)),
            );
            rows.push(Carrier {
                key,
                cells,
                docs: vec![],
            });
            rows.len() - 1
        });
        let row = &mut rows[idx];
        row.docs.push(doc);
        row.cells[1] = json!(row.docs.len());
    }
    let needle = format::fold(search.trim());
    if !needle.is_empty() {
        rows.retain(|r| {
            format::fold(&r.cells.iter().map(cell).collect::<Vec<_>>().join(" ")).contains(&needle)
        });
    }
    rows.sort_by_cached_key(|r| format::fold(r.cells[0].as_str().unwrap_or("")));
    rows
}
fn decode(input: &str) -> String {
    let raw = input.as_bytes();
    let mut out = Vec::with_capacity(raw.len());
    let mut i = 0;
    while i < raw.len() {
        if raw[i] == b'%' && i + 2 < raw.len() {
            if let (Some(a), Some(b)) = (
                (raw[i + 1] as char).to_digit(16),
                (raw[i + 2] as char).to_digit(16),
            ) {
                out.push((a * 16 + b) as u8);
                i += 3;
                continue;
            }
        }
        out.push(if raw[i] == b'+' { b' ' } else { raw[i] });
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}
pub fn query_encode(input: &str) -> String {
    let mut value = String::new();
    use std::fmt::Write;
    for b in input.bytes() {
        if b.is_ascii_alphanumeric() || b"-._~".contains(&b) {
            value.push(b as char);
        } else {
            write!(value, "%{b:02X}").unwrap();
        }
    }
    value
}
fn first_param(query: &str, key: &str) -> String {
    query
        .split('&')
        .filter_map(|pair| pair.split_once('='))
        .find_map(|(k, v)| (!v.is_empty() && decode(k) == key).then(|| decode(v)))
        .unwrap_or_default()
}
pub fn directory_response(
    state: Option<&State>,
    method: &str,
    target: &str,
) -> Option<HttpResponse> {
    directory_response_from_data(state, method, target, fixture(), fixture().snapshot_at)
}
pub fn directory_response_from_data(
    state: Option<&State>,
    method: &str,
    target: &str,
    data: &DirectoryData,
    as_of: NaiveDateTime,
) -> Option<HttpResponse> {
    let (path, query) = target.split_once('?').unwrap_or((target, ""));
    let section = match path {
        "/api/carriers" | "/api/carriers.xlsx" => "carriers",
        "/api/supplier-directory" | "/api/supplier-directory.xlsx" => "suppliers",
        _ => return None,
    };
    let Some(state) = state else {
        return Some(HttpResponse::json(401, json!({"error":"Требуется вход"})));
    };
    if !permitted(state, section) {
        return Some(HttpResponse::json(
            403,
            json!({"error":"Раздел закрыт для вашего отдела"}),
        ));
    }
    if method != "GET" {
        return Some(HttpResponse::json(404, json!({"error":"Не найдено"})));
    }
    let export = path.ends_with(".xlsx");
    let search = first_param(query, "search");
    Some(if section == "carriers" {
        let records = carriers(data, if export { &search } else { "" });
        if export {
            let rows = records
                .iter()
                .map(|r| r.cells.iter().map(cell).collect())
                .collect::<Vec<_>>();
            HttpResponse::excel(
                "carriers.xlsx",
                xlsx::workbook("Перевозчики", &CARRIER_COLUMNS, &rows),
            )
        } else {
            HttpResponse::json(
                200,
                json!({"columns":CARRIER_COLUMNS,"rows":records.into_iter().map(|r|r.cells).collect::<Vec<_>>()}),
            )
        }
    } else {
        if export && data.meta.synced_at.as_deref().unwrap_or("").is_empty() {
            return Some(HttpResponse::json(
                409,
                json!({"error":"Дождитесь первой загрузки базы поставщиков из 1С"}),
            ));
        }
        let records = suppliers(data, &search, as_of);
        if export {
            let rows = records
                .iter()
                .map(|r| {
                    supplier_columns::COLUMNS
                        .iter()
                        .map(|h| r.get(h).to_string())
                        .collect()
                })
                .collect::<Vec<_>>();
            HttpResponse::excel(
                "supplier-directory.xlsx",
                xlsx::workbook("Лист 1", &supplier_columns::COLUMNS, &rows),
            )
        } else {
            let meta = json!({"synced_at":data.meta.synced_at,"error":data.meta.error.as_deref().filter(|s|!s.is_empty())});
            HttpResponse::json(
                200,
                json!({"mode":"directory","list":records,"meta":meta,"columns":DISPLAY,"refreshing":data.refreshing}),
            )
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn normalizing_names_regions_and_search_preserves_business_fields() {
        for (raw, expected) in [
            (
                "Общество с ограниченной ответственностью ГК «Ромашка»",
                "Ромашка, ГК ООО",
            ),
            ("ПАО «Завод»", "Завод, ПАО"),
            ("ОООмега", "ОООмега"),
            ("ООО  «Мой „ТД“»", "Мой „“, ТД ООО"),
            ("ООО (ГК) «Опора»", "Опора, ГК ООО"),
        ] {
            assert_eq!(format::company(raw), expected);
        }
        assert_eq!(
            format::region("Пермь; Пермский край; Челябинская обл").as_deref(),
            Some("59-Пермский край; 74-Челябинская область")
        );
        assert!(format::region("Казань; неизвестно").is_none());
        assert_eq!(format::fold("STRAßE Σς"), "strasse σσ");
        let r = suppliers(fixture(), "КРЕПЕЖ 0105", fixture().snapshot_at);
        assert_eq!(r.len(), 1);
        assert_eq!(r[0].get("Регион"), "16-Республика Татарстан");
    }

    #[test]
    fn enrichment_window_has_an_inclusive_thirty_day_boundary() {
        let mut data = fixture().clone();
        let row = &mut data.enrichment[1];
        row.checked_at = "2026-10-24T12:00:00".into();
        assert_eq!(
            suppliers(&data, "0106", data.snapshot_at)[0].get("Регион"),
            "52-Нижегородская область"
        );
        data.enrichment[1].checked_at = "2026-10-24T11:59:59".into();
        assert_eq!(
            suppliers(&data, "0106", data.snapshot_at)[0].get("Регион"),
            "Уточняется"
        );
    }

    #[test]
    fn cancelled_unnamed_and_older_carrier_orders_do_not_supply_current_fields() {
        let mut data = fixture().clone();
        let mut tied = data.forwarders[1].clone();
        tied.dt = Some("2027-01-01".into());
        tied.doc = Some("ПЭ-test".into());
        tied.created_at = None;
        tied.route = Some("старый по порядку".into());
        data.forwarders.push(tied.clone());
        tied.created_at = Some("2027-01-01T13:00:00".into());
        tied.route = None;
        data.forwarders.push(tied);
        let rows = carriers(&data, "");
        assert_eq!(rows.len(), 3);
        let row = rows.iter().find(|c| c.key == "carrier-1").unwrap();
        assert_eq!(row.docs.len(), 5);
        assert_eq!(row.cells[4], Value::Null);
        assert_eq!(
            rows.iter()
                .find(|c| c.key == "рельсовый путь, ооо")
                .unwrap()
                .docs
                .len(),
            4
        );
    }

    #[test]
    fn exports_require_visible_section_and_first_successful_sync() {
        let admin = State::default();
        let mut employee = admin.clone();
        employee.role = "employee".into();
        let mut data = fixture().clone();
        data.meta.synced_at = None;
        for (state, status) in [(None, 401), (Some(&employee), 403), (Some(&admin), 409)] {
            let got = directory_response_from_data(
                state,
                "GET",
                "/api/supplier-directory.xlsx",
                &data,
                data.snapshot_at,
            )
            .unwrap();
            assert_eq!(got.status, status);
            assert!(got.filename.is_none());
        }
        employee
            .values
            .insert("access/Отдел продаж/suppliers".into(), "1".into());
        let response = directory_response(
            Some(&employee),
            "GET",
            "/api/supplier-directory?search=&search=%D0%BE%D0%BF%D0%BE%D1%80%D0%B0&search=missing",
        )
        .unwrap();
        assert_eq!(response.status, 200);
        let json: Value = serde_json::from_slice(&response.body).unwrap();
        assert_eq!(json["list"].as_array().unwrap().len(), 1);
        assert_eq!(json["list"][0]["id"], "vendor-1");
    }
}
