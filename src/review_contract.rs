//! The original dashboard's per-record KPI approval and exclusion requests.
use crate::{employees, kpi, sandbox::Reply, State};
use serde_json::{json, Value};

pub fn handle(state: &mut State, method: &str, target: &str, body: &Value) -> Option<Reply> {
    let path = target.split('?').next()?;
    if !matches!(path, "/api/kpi-decision" | "/api/kpi-exclusion") || method != "POST" {
        return None;
    }
    Some(match apply(state, path, body) {
        Ok(()) => Reply::json(200, json!({"ok":true})),
        Err((status, message)) => Reply::error(status, message),
    })
}

type Error = (u16, &'static str);

fn apply(state: &mut State, path: &str, body: &Value) -> Result<(), Error> {
    if state.role != "admin" {
        return Err((403, "Изменять данные может только руководитель"));
    }
    let field = |name: &str| {
        body.get(name)
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
    };
    let emp = field("emp")
        .filter(|id| employees().iter().any(|e| e.0 == *id) && *id != "director")
        .ok_or((400, "Сотрудник не найден"))?;
    let month = field("month")
        .filter(|m| {
            let b = m.as_bytes();
            b.len() == 7
                && b[..4].iter().all(u8::is_ascii_digit)
                && b[4] == b'-'
                && matches!(
                    &b[5..],
                    b"01"
                        | b"02"
                        | b"03"
                        | b"04"
                        | b"05"
                        | b"06"
                        | b"07"
                        | b"08"
                        | b"09"
                        | b"10"
                        | b"11"
                        | b"12"
                )
        })
        .ok_or((400, "Месяц указывается в формате ГГГГ-ММ"))?;
    let (Some(code), Some(kind), Some(id), Some(value)) = (
        field("code"),
        field("kind"),
        field("ref"),
        body.get("value"),
    ) else {
        return Err((400, "Не указана запись KPI или решение"));
    };
    let targets = kpi::review_targets(state, emp, month, code, kind, id).ok_or((
        400,
        "Запись не входит в этот KPI сотрудника за выбранный месяц",
    ))?;
    if path == "/api/kpi-exclusion" {
        let excluded = value
            .as_bool()
            .ok_or((400, "Не указано действие с записью"))?;
        for slot in targets {
            kpi::exclude(state, &slot, excluded);
        }
    } else {
        let decision = match value {
            Value::Null => "auto",
            v if v.as_i64() == Some(1) => "yes",
            v if v.as_i64() == Some(0) => "no",
            _ => return Err((400, "Выберите Авто, Да или Нет")),
        };
        kpi::approve(state, &targets[0], decision);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn body(id: &str, value: Value) -> Value {
        json!({"emp":"logistics","month":"2026-11","code":"logistics_documents",
            "kind":"forwarder_order","ref":id,"value":value})
    }

    #[test]
    fn review_uses_the_ui_record_and_survives_exclusion_and_restore() {
        let mut state = State {
            person: "logistics".into(),
            route: "kpi".into(),
            selected: "logistics_documents".into(),
            ..State::default()
        };
        let id = "demo-f-0-2"; // A route sheet is attached, with no contract.
        let payout = |s: &State| kpi::monthly_payout(s, "logistics").unwrap();
        let initial = payout(&state);
        assert_eq!(
            handle(&mut state, "POST", "/api/kpi-decision", &body(id, json!(1)))
                .unwrap()
                .status,
            200
        );
        let approved = payout(&state);
        assert!(approved > initial);
        let page = kpi::render(&state);
        let row = page
            .split_once(r#"data-review-ref="demo-f-0-2""#)
            .unwrap()
            .1
            .split_once("</tr>")
            .unwrap()
            .0;
        assert!(row.contains(r#"<option value="yes" selected>"#));
        let action_id = row
            .split_once("data-act=\"kpi-review:")
            .unwrap()
            .1
            .split_once('"')
            .unwrap()
            .0;
        assert!(!kpi::action(&mut state, "kpi-remove", action_id));
        assert!(kpi::action(&mut state, "kpi-ask", action_id));
        assert!(kpi::action(&mut state, "kpi-remove", action_id));
        let excluded = payout(&state);
        assert_eq!(
            handle(&mut state, "POST", "/api/kpi-decision", &body(id, json!(0)))
                .unwrap()
                .status,
            200
        );
        assert_eq!(payout(&state), excluded);
        assert_eq!(
            handle(
                &mut state,
                "POST",
                "/api/kpi-exclusion",
                &body(id, json!(false))
            )
            .unwrap()
            .status,
            200
        );
        assert_eq!(payout(&state), initial);
        assert!(kpi::action(
            &mut state,
            &format!("kpi-review:{action_id}"),
            "yes"
        ));
        assert_eq!(payout(&state), approved);
        assert_eq!(
            handle(
                &mut state,
                "POST",
                "/api/kpi-decision",
                &body(id, Value::Null)
            )
            .unwrap()
            .status,
            200
        );
        assert_eq!(payout(&state), initial);
    }

    #[test]
    fn invalid_or_forbidden_reviews_preserve_all_state() {
        let mut state = State::default();
        let valid = body("demo-f-0-1", json!(1));
        for (key, value) in [
            ("emp", json!("sales")),
            ("month", json!("2026-10")),
            ("month", json!("2026-13")),
            ("code", json!("logistics_budget")),
            ("kind", json!("customer_order")),
            ("ref", json!("cancelled")),
            ("ref", json!("unnamed")),
            ("ref", json!(1)),
            ("value", json!(true)),
            ("value", json!(1.0)),
        ] {
            let mut request = valid.clone();
            request[key] = value;
            assert_eq!(
                handle(&mut state, "POST", "/api/kpi-decision", &request)
                    .unwrap()
                    .status,
                400
            );
        }
        assert_eq!(
            handle(&mut state, "POST", "/api/kpi-exclusion", &valid)
                .unwrap()
                .status,
            400
        );
        state.role = "employee".into();
        assert_eq!(
            handle(&mut state, "POST", "/api/kpi-decision", &valid)
                .unwrap()
                .status,
            403
        );
        assert!(state.values.is_empty());
    }

    #[test]
    fn a_week_becomes_reviewable_when_a_score_is_entered() {
        let mut state = State {
            person: "logistics".into(),
            route: "kpi".into(),
            selected: "logistics_cargo_safety".into(),
            ..State::default()
        };
        let request = json!({"emp":"logistics","month":"2026-11","code":"logistics_cargo_safety",
            "kind":"cargo_safety_week","ref":"2026-11-09","value":1});
        let review = |s: &mut State| {
            handle(s, "POST", "/api/kpi-decision", &request)
                .unwrap()
                .status
        };
        assert_eq!(review(&mut state), 400);
        assert!(!kpi::action(&mut state, "kpi-review:0/1", "yes"));
        assert!(kpi::action(&mut state, "kpi-score:0/1", "0"));
        assert_eq!(review(&mut state), 200);
        assert!(kpi::action(&mut state, "kpi-score:0/1", ""));
        assert_eq!(review(&mut state), 400);
    }

    #[test]
    fn legacy_receivables_share_exclusion_and_keep_separate_decisions() {
        let mut state = State::default();
        let request = |kind: &str, value| {
            json!({"emp":"sales","month":"2026-09","code":"receivables_pm",
            "kind":kind,"ref":"Заказ клиента Д-202609-001","value":value})
        };
        let key =
            |kind| format!("review/2026-09/sales/receivables_pm/{kind}/Заказ клиента Д-202609-001");
        for (kind, value) in [("sale_due", 1), ("payment_on_time", 0)] {
            assert_eq!(
                handle(
                    &mut state,
                    "POST",
                    "/api/kpi-decision",
                    &request(kind, json!(value))
                )
                .unwrap()
                .status,
                200
            );
        }
        assert_eq!(
            handle(
                &mut state,
                "POST",
                "/api/kpi-exclusion",
                &request("sale_due", json!(true))
            )
            .unwrap()
            .status,
            200
        );
        assert_eq!(state.values[&key("sale_due")], "excluded");
        assert_eq!(state.values[&key("payment_on_time")], "excluded");
        assert_eq!(
            handle(
                &mut state,
                "POST",
                "/api/kpi-exclusion",
                &request("payment_on_time", json!(false))
            )
            .unwrap()
            .status,
            200
        );
        assert_eq!(state.values[&key("sale_due")], "yes");
        assert_eq!(state.values[&key("payment_on_time")], "no");
    }
}
