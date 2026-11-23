use chrono::{DateTime, Datelike, NaiveDate, NaiveDateTime, TimeDelta, Timelike};
use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Deserialize)]
pub struct Request {
    pub history: Option<String>,
    pub status: String,
    pub now: String,
    pub processing_minutes: i64,
}

#[derive(Serialize)]
pub struct Timing {
    pub received: Option<String>,
    pub finished: Option<String>,
    pub duration: String,
    pub category: &'static str,
    pub result: &'static str,
}

// The transport feed uses ISO calendar dates and date-times. Naive timestamps
// already represent Yekaterinburg; only timestamps with an offset are shifted.
fn local_datetime(value: &str) -> Option<NaiveDateTime> {
    let value = value.trim();
    if value.starts_with("0001") || value.starts_with("9999") || value.ends_with('z') {
        return None;
    }
    let normalized = value.replace(' ', "T").replace(',', ".");
    let local = if let Ok(aware) = DateTime::parse_from_rfc3339(&normalized) {
        aware.naive_utc().checked_add_signed(TimeDelta::hours(5))?
    } else if let Ok(aware) = DateTime::parse_from_str(&normalized, "%Y-%m-%dT%H:%M:%S%.f%z") {
        aware.naive_utc().checked_add_signed(TimeDelta::hours(5))?
    } else if let Ok(datetime) = NaiveDateTime::parse_from_str(&normalized, "%Y-%m-%dT%H:%M:%S%.f")
    {
        datetime
    } else if let Ok(datetime) = NaiveDateTime::parse_from_str(&normalized, "%Y-%m-%dT%H:%M") {
        datetime
    } else {
        NaiveDate::parse_from_str(&normalized, "%Y-%m-%d")
            .ok()?
            .and_hms_opt(0, 0, 0)?
    };
    if !(1..=9999).contains(&local.year()) || local.nanosecond() >= 1_000_000_000 {
        return None;
    }
    local.with_nanosecond(local.nanosecond() / 1000 * 1000)
}

fn isoformat(value: NaiveDateTime) -> String {
    let format = if value.nanosecond() == 0 {
        "%Y-%m-%dT%H:%M:%S"
    } else {
        "%Y-%m-%dT%H:%M:%S%.6f"
    };
    value.format(format).to_string()
}

#[derive(Default)]
struct StatusHistory {
    received: Option<NaiveDateTime>,
    finished: Option<NaiveDateTime>,
    invalid: bool,
}

impl StatusHistory {
    fn parse(raw: Option<&str>) -> Self {
        let mut history = Self::default();
        let Ok(value) = serde_json::from_str::<Value>(raw.unwrap_or("")) else {
            return history;
        };
        let Some(events) = value.as_array() else {
            history.invalid = true;
            return history;
        };
        for event in events {
            let Some([name, value]) = event.as_array().map(Vec::as_slice) else {
                history.invalid = true;
                continue;
            };
            match name.as_str() {
                Some(status @ ("Поступила" | "Отработана")) => {
                    let Some(moment) = value.as_str().and_then(local_datetime) else {
                        history.invalid = true;
                        continue;
                    };
                    if status == "Поступила" {
                        history.received =
                            Some(history.received.map_or(moment, |first| first.min(moment)));
                    } else {
                        history.finished =
                            Some(history.finished.map_or(moment, |last| last.max(moment)));
                    }
                }
                None => history.invalid = true,
                Some(_) => {}
            }
        }
        history
    }
}

pub fn request_timing(request: Request) -> Result<Timing, &'static str> {
    let now = local_datetime(&request.now).ok_or("invalid calculation timestamp")?;
    let limit = TimeDelta::try_minutes(request.processing_minutes)
        .ok_or("processing limit out of range")?;
    let history = StatusHistory::parse(request.history.as_deref());
    let mut timing = Timing {
        received: history.received.map(isoformat),
        finished: history.finished.map(isoformat),
        duration: String::new(),
        category: "unknown",
        result: "Нет данных",
    };
    timing.duration = match (history.received, history.finished) {
        (None, None) => "Нет дат статусов".into(),
        _ if history.invalid => "Некорректная история статусов".into(),
        (None, _) => "Нет даты «Поступила»".into(),
        (Some(start), _) if start > now => "Дата «Поступила» ещё не наступила".into(),
        (Some(start), None) => {
            if request.status != "Отработана" {
                (timing.category, timing.result) = if now - start > limit {
                    ("late", "Не отработана, срок истёк")
                } else {
                    ("pending", "Время ещё есть")
                };
            }
            "Нет даты «Отработана»".into()
        }
        (Some(start), Some(end)) if end < start => "«Отработана» раньше «Поступила»".into(),
        (_, Some(end)) if end > now => "Дата «Отработана» ещё не наступила".into(),
        (Some(start), Some(end)) => {
            let elapsed = end - start;
            (timing.category, timing.result) = if elapsed <= limit {
                ("good", "В норматив")
            } else {
                ("late", "Просрочена")
            };
            let seconds = elapsed.num_seconds();
            format!(
                "{} ч {:02} мин {:02} с",
                seconds / 3600,
                seconds / 60 % 60,
                seconds % 60
            )
        }
    };
    Ok(timing)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn evaluate(events: Value, now: &str) -> Timing {
        request_timing(Request {
            history: Some(events.to_string()),
            status: "В работе".into(),
            now: now.into(),
            processing_minutes: 1440,
        })
        .unwrap()
    }

    #[test]
    fn unsorted_repeated_statuses_use_earliest_arrival_and_last_completion() {
        let events = json!([
            ["Отработана", "2026-11-02T03:00:00.100001Z"],
            ["Поступила", "2026-11-01T08:20:00"],
            ["Отработана", "2026-11-02T08:00:00+05:00"],
            ["Поступила", "2026-11-01T08:00:00.100000"]
        ]);
        let timing = evaluate(events, "2026-11-02T09:00:00");
        assert_eq!(
            timing.received.as_deref(),
            Some("2026-11-01T08:00:00.100000")
        );
        assert_eq!(
            timing.finished.as_deref(),
            Some("2026-11-02T08:00:00.100001")
        );
        assert_eq!(timing.duration, "24 ч 00 мин 00 с");
        assert_eq!(timing.category, "late");
    }

    #[test]
    fn incomplete_request_is_still_pending_at_the_deadline() {
        let events = json!([["Поступила", "2026-11-01T08:00:00"]]);
        assert_eq!(
            evaluate(events.clone(), "2026-11-02T08:00:00").category,
            "pending"
        );
        assert_eq!(
            evaluate(events, "2026-11-02T08:00:00.000001").category,
            "late"
        );
    }

    #[test]
    fn malformed_event_does_not_erase_valid_timestamps_or_earn_credit() {
        let events = json!([
            ["Поступила", "2026-11-01"],
            ["Отработана", "9999-12-31T23:59:59"]
        ]);
        let timing = evaluate(events, "2026-11-03");
        assert_eq!(timing.category, "unknown");
        assert_eq!(timing.duration, "Некорректная история статусов");
        assert!(timing.received.is_some());
        assert!(timing.finished.is_none());
    }
}
