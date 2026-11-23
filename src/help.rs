use crate::{employees, esc, viewer, State};
use serde::{Deserialize, Serialize};
use std::fmt::Write;

const TICKET_PREFIX: &str = "support/ticket/";
const ATTACHMENT_LIMIT: usize = 500_000;
const TICKETS_BUDGET: usize = 2_500_000;

#[derive(Serialize, Deserialize, Default)]
struct Submission {
    text: String,
    #[serde(default)]
    images: Vec<Image>,
}

#[derive(Serialize, Deserialize)]
struct Image {
    name: String,
    url: String,
}

impl Image {
    fn valid(&self) -> bool {
        let Some((media, data)) = self.url.split_once(',') else {
            return false;
        };
        let has_signature = match media {
            "data:image/png;base64" => data.starts_with("iVBORw0KGgo"),
            "data:image/jpeg;base64" => data.starts_with("/9j/"),
            "data:image/webp;base64" => {
                data.starts_with("UklGR") && data.get(11..16) == Some("XRUJQ")
            }
            _ => false,
        };
        has_signature
            && !self.name.is_empty()
            && self.name.chars().count() <= 160
            && data.len() <= ATTACHMENT_LIMIT
            && data.len() % 4 == 0
            && data
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'/' | b'+' | b'='))
    }
}

#[derive(Serialize, Deserialize)]
struct Ticket {
    sender: String,
    recipient: String,
    submission: Submission,
}

fn name(id: &str) -> &'static str {
    employees()
        .into_iter()
        .find(|e| e.0 == id)
        .map_or("Сотрудник", |e| e.1)
}
fn recipient(s: &State) -> &str {
    s.values
        .get("help/recipient")
        .map(String::as_str)
        .filter(|id| employees().iter().any(|e| e.0 == *id))
        .unwrap_or("director")
}
fn summary(text: &str) -> String {
    let sentence = text
        .trim()
        .split_terminator(['\n', '.', '!', '?'])
        .find(|s| !s.trim().is_empty())
        .unwrap_or(text)
        .trim();
    if sentence.chars().count() <= 90 {
        sentence.to_owned()
    } else {
        format!(
            "{}…",
            sentence.chars().take(89).collect::<String>().trim_end()
        )
    }
}

pub fn save(s: &mut State, value: &str) {
    if s.values.get("ui-dialog").map(String::as_str) != Some("help") {
        return;
    }
    let Ok(mut input) = serde_json::from_str::<Submission>(value) else {
        return;
    };
    let sender = viewer(s).to_owned();
    let text = input.text.trim();
    // Keep the description editable if the file or storage validation fails.
    s.values.insert(
        format!("support/draft/{sender}"),
        text.chars().take(2000).collect(),
    );
    let error = if text.is_empty() || text.chars().count() > 2000 {
        "Введите описание длиной до 2000 символов."
    } else if input.images.len() > 3 || !input.images.iter().all(Image::valid) {
        "Можно приложить до трёх изображений PNG, JPEG или WebP. Уменьшите файл или отправьте описание отдельно."
    } else {
        ""
    };
    if !error.is_empty() {
        s.values.insert("support/error".into(), error.into());
        return;
    }
    input.text = input.text.trim().to_owned();
    let next = s
        .values
        .keys()
        .filter_map(|k| k.strip_prefix(TICKET_PREFIX)?.parse::<u64>().ok())
        .max()
        .unwrap_or(0)
        + 1;
    let ticket = Ticket {
        sender: sender.clone(),
        recipient: recipient(s).to_owned(),
        submission: input,
    };
    let json = serde_json::to_string(&ticket).unwrap_or_default();
    let occupied = s
        .values
        .iter()
        .filter(|(k, _)| k.starts_with(TICKET_PREFIX))
        .map(|(_, v)| v.len())
        .sum::<usize>();
    if occupied + json.len() > TICKETS_BUDGET {
        s.values.insert(
            "support/error".into(),
            "Хранилище обращений этого тестового сеанса заполнено. Уменьшите вложения.".into(),
        );
        return;
    }
    s.values.insert(format!("{TICKET_PREFIX}{next:08}"), json);
    s.values.remove(&format!("support/draft/{sender}"));
    s.values.remove("support/error");
    s.values.insert("ui-dialog".into(), "help-saved".into());
}

pub fn inbox(s: &State) -> String {
    let mut rows = String::new();
    let mut count = 0;
    for (_, data) in s
        .values
        .iter()
        .rev()
        .filter(|(k, _)| k.starts_with(TICKET_PREFIX))
    {
        let Ok(t) = serde_json::from_str::<Ticket>(data) else {
            continue;
        };
        if t.recipient != viewer(s) {
            continue;
        }
        count += 1;
        let sender = esc(name(&t.sender));
        let short = esc(&summary(&t.submission.text));
        write!(rows,"<details><summary><strong>{sender}</strong> · {short}</summary><h4>Проблема в работе дашборда</h4><p>Пользователь: {sender}<br>Суть проблемы: {short}</p><p>Текст пользователя: {}</p>",esc(&t.submission.text).replace('\n',"<br>")).unwrap();
        for file in t.submission.images.iter().filter(|i| i.valid()) {
            write!(rows,"<figure><img loading=\"lazy\" src=\"{}\" alt=\"{}\" style=\"max-width:100%;max-height:320px;object-fit:contain\"><figcaption>{}</figcaption></figure>",file.url,esc(&file.name),esc(&file.name)).unwrap();
        }
        rows += "</details>";
    }
    if count == 0 {
        return String::new();
    }
    format!("<section class=\"panel support-inbox\"><h2>Входящие обращения · {count}</h2>{rows}</section>")
}

pub fn dialog(s: &State) -> String {
    let dest = esc(name(recipient(s)));
    let inside=match s.values.get("ui-dialog").map(String::as_str) {
        Some("help") => {
            let draft_key=format!("support/draft/{}",viewer(s));
            let text=esc(s.values.get(&draft_key).map(String::as_str).unwrap_or(""));
            let error=esc(s.values.get("support/error").map(String::as_str).unwrap_or(""));
            let messages=inbox(s);
            format!(r#"<h2>Помощь</h2><p>Получатель · {dest}. Обращение появится во входящих этого профиля в тестовой копии.</p><form data-act="help-save"><label>Текст обращения<textarea data-value-field maxlength="2000" required rows="4">{text}</textarea></label><label>Скриншоты <input type="file" data-help-files multiple accept="image/png,image/jpeg,image/webp"></label><p class="muted">Можно вставить скриншот из буфера обмена. До трёх изображений.</p><ul data-help-list></ul><p role="status" data-help-status>{error}</p><button class="btn primary" type="submit">Сохранить обращение</button></form>{messages}"#)
        },
        Some("help-saved")=>format!("<h2>Обращение сохранено</h2><p>Получатель · {dest}. Обращение доступно получателю в этом тестовом сеансе.</p>"),
        _=>return String::new(),
    };
    format!(
        r#"<div class="veil"><section class="modal panel" style="max-height:85vh;overflow:auto" role="dialog" aria-modal="true" aria-label="Помощь"><button type="button" class="modal-x" data-act="dialog" data-val="" aria-label="Закрыть">×</button>{inside}</section></div>"#
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn support_routes_to_selected_recipient_and_keeps_sender() {
        let mut s = State::default();
        s.role = "employee".into();
        s.values.insert("viewer".into(), "sales".into());
        s.values.insert("help/recipient".into(), "supply".into());
        s.values.insert("ui-dialog".into(), "help".into());
        save(&mut s, r#"{"text":"Не вижу закрытую заявку."}"#);
        assert_eq!(s.values["ui-dialog"], "help-saved");
        assert!(inbox(&s).is_empty());
        s.values.insert("viewer".into(), "supply".into());
        let received = inbox(&s);
        assert!(received.contains(name("sales")) && received.contains("Не вижу закрытую заявку."));
    }
}
