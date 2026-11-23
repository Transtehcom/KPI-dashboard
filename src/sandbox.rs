use crate::{apply, http_contract, page_for, page_html, title, State};
use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

pub const MAX_INPUT: usize = 3 * 1024 * 1024;

#[derive(Deserialize)]
pub struct Request {
    pub saved: Option<String>,
    pub revision: u64,
    pub method: String,
    pub target: String,
    pub body: Value,
}

#[derive(Serialize)]
pub struct Reply {
    pub status: u16,
    pub content_type: &'static str,
    pub body: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub encoding: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub filename: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub saved: Option<String>,
}

impl Reply {
    pub fn json(status: u16, payload: Value) -> Self {
        Self {
            status,
            content_type: "application/json; charset=utf-8",
            body: payload.to_string(),
            saved: None,
            filename: None,
            encoding: None,
        }
    }
    pub fn error(status: u16, message: &str) -> Self {
        Self::json(status, json!({"error":message}))
    }
}

pub fn handle_bytes(bytes: &[u8]) -> Reply {
    match serde_json::from_slice(bytes) {
        Ok(request) => handle(request),
        Err(_) => Reply::error(400, "Некорректный запрос"),
    }
}

pub fn handle(req: Request) -> Reply {
    let mut saved = match req
        .saved
        .as_deref()
        .map(serde_json::from_str::<State>)
        .transpose()
    {
        Ok(state) => state,
        Err(_) => return Reply::error(500, "Не удалось прочитать сохранённый сеанс"),
    };
    if !matches!(
        req.target.as_str(),
        "/api/sandbox/session" | "/api/sandbox/action"
    ) {
        let path = req.target.split('?').next().unwrap_or("");
        if matches!(path, "/api/kpi-decision" | "/api/kpi-exclusion") {
            let Some(state) = saved.as_mut() else {
                return Reply::error(401, "Требуется вход");
            };
            if let Some(mut reply) =
                crate::review_contract::handle(state, &req.method, path, &req.body)
            {
                if reply.status == 200 && req.method == "POST" {
                    reply.saved =
                        Some(serde_json::to_string(state).expect("serializable dashboard state"));
                }
                return reply;
            }
        }
        if let Some(r) = http_contract::directory_response(saved.as_ref(), &req.method, &req.target)
        {
            let is_file = r.filename.is_some();
            return Reply {
                status: r.status,
                content_type: r.content_type,
                filename: r.filename,
                saved: None,
                encoding: is_file.then_some("base64"),
                body: if is_file {
                    STANDARD.encode(r.body)
                } else {
                    match String::from_utf8(r.body) {
                        Ok(text) => text,
                        Err(_) => return Reply::error(500, "Некорректный ответ справочника"),
                    }
                },
            };
        }
        return Reply::error(404, "Маршрут отсутствует");
    }
    if req.method != "POST" {
        return Reply::error(405, "Требуется POST");
    }
    let mut state = match saved {
        Some(state) => state,
        None if req.target.ends_with("/session") => State::default(),
        None => return Reply::error(401, "Откройте дашборд для начала сеанса"),
    };
    if req.target.ends_with("/session") {
        let route = req
            .body
            .get("route")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .unwrap_or("home");
        let time = req.body.get("time").and_then(Value::as_str).unwrap_or("");
        // Session state always comes from storage selected by the server.
        apply(
            &mut state,
            "init",
            &json!({"route":route,"time":time}).to_string(),
        );
    } else {
        let Some(base) = req.body.get("revision").and_then(Value::as_u64) else {
            return Reply::error(400, "Требуется версия страницы");
        };
        if base != req.revision {
            return Reply::error(409, "Данные изменились в другой вкладке");
        }
        let key = req.body.get("key").and_then(Value::as_str).unwrap_or("");
        let Some(value) = req.body.get("value").and_then(Value::as_str) else {
            return Reply::error(400, "Требуется значение действия");
        };
        if key == "init" || key.is_empty() || key.len() > 400 || value.len() > MAX_INPUT {
            return Reply::error(400, "Недопустимое действие");
        }
        apply(&mut state, key, value);
    }
    let mut reply = Reply::json(
        200,
        json!({
            "html":page_html(&state),"route":state.route, "revision":req.revision+1,
            "title":page_for(&state.route).map(|page|title(&state,page)).unwrap_or("Главное меню"),
            "theme":state.values.get("theme").map(String::as_str).unwrap_or("light")
        }),
    );
    reply.saved = Some(serde_json::to_string(&state).expect("serializable dashboard state"));
    reply
}

#[cfg(test)]
mod tests {
    use super::*;
    fn request(target: &str, saved: Option<String>, revision: u64, body: Value) -> Reply {
        handle(Request {
            target: target.into(),
            method: "POST".into(),
            saved,
            revision,
            body,
        })
    }
    #[test]
    fn restored_state_cannot_be_submitted_by_a_client() {
        let r = request(
            "/api/sandbox/session",
            None,
            0,
            json!({"route":"home","saved":"{\"period\":\"2026-01\"}"}),
        );
        let s: State = serde_json::from_str(r.saved.as_ref().unwrap()).unwrap();
        assert_eq!(s.period, "2026-11");
        let r = request(
            "/api/sandbox/action",
            r.saved,
            1,
            json!({"key":"init","value":"{}","revision":1}),
        );
        assert_eq!(r.status, 400);
        assert!(r.saved.is_none());
    }
    #[test]
    fn writes_require_the_current_session_revision() {
        let r = request("/api/sandbox/session", None, 0, json!({"route":"money"}));
        let update = json!({"key":"theme","value":"","revision":1});
        assert_eq!(
            request("/api/sandbox/action", None, 1, update.clone()).status,
            401
        );
        assert_eq!(
            request("/api/sandbox/action", r.saved.clone(), 2, update.clone()).status,
            409
        );
        let r = request("/api/sandbox/action", r.saved, 1, update);
        let view: Value = serde_json::from_str(&r.body).unwrap();
        assert_eq!(view["theme"], "dark");
        assert_eq!(view["revision"], 2);
        assert!(view.get("saved").is_none());
    }
}
