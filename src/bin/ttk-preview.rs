//! Loopback-only server for the same Rust application used by the private Site.
use base64::{engine::general_purpose::STANDARD, Engine as _};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::{Arc, Mutex};
use tiny_http::{Header, Request, Response, Server, StatusCode};
use ttk_dashboard_site::sandbox::{self, Reply};

#[derive(Deserialize, Serialize)]
struct Session {
    revision: u64,
    state: String,
}

struct Store {
    path: PathBuf,
    _guard: File,
    writes: Mutex<()>,
}
impl Store {
    fn open(path: PathBuf) -> io::Result<Self> {
        fs::create_dir_all(&path)?;
        let guard = File::options()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(path.join("server.lock"))?;
        guard.try_lock().map_err(|e| {
            io::Error::other(format!(
                "Не удалось занять хранилище тестового сервера: {e}"
            ))
        })?;
        Ok(Self {
            path,
            _guard: guard,
            writes: Mutex::new(()),
        })
    }
    fn load(&self, token: &str) -> io::Result<Option<Session>> {
        match File::open(self.path.join(format!("{token}.json"))) {
            Ok(f) => Ok(Some(serde_json::from_reader(f).map_err(io::Error::other)?)),
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e),
        }
    }
    fn save(&self, token: &str, session: Session) -> io::Result<()> {
        let pending = self.path.join(format!("{}.tmp", new_token()?));
        let mut f = File::create_new(&pending)?;
        let result = (|| {
            serde_json::to_writer(&mut f, &session)?;
            f.flush()?;
            f.sync_all()?;
            drop(f);
            fs::rename(&pending, self.path.join(format!("{token}.json")))
        })();
        if result.is_err() {
            let _ = fs::remove_file(pending);
        }
        result
    }
}

fn header<'a>(req: &'a Request, name: &'static str) -> Option<&'a str> {
    req.headers()
        .iter()
        .find(|h| h.field.equiv(name))
        .map(|h| h.value.as_str())
}
fn token_in(req: &Request) -> Option<String> {
    header(req, "Cookie")?
        .split(';')
        .map(str::trim)
        .filter_map(|c| c.strip_prefix("kpi_session="))
        .find(|v| {
            v.len() == 64
                && v.bytes()
                    .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        })
        .map(str::to_owned)
}
fn new_token() -> io::Result<String> {
    let mut bytes = [0u8; 32];
    getrandom::getrandom(&mut bytes).map_err(|e| io::Error::other(e.to_string()))?;
    Ok(bytes.iter().map(|b| format!("{b:02x}")).collect())
}
fn api(req: &mut Request, store: &Store) -> io::Result<(Reply, Option<String>)> {
    let method = req.method().as_str().to_owned();
    let target = req.url().to_owned();
    let body = if method == "POST" {
        if let Some(origin) = header(req, "Origin") {
            if header(req, "Host").is_none_or(|host| origin != format!("http://{host}")) {
                return Ok((Reply::error(403, "Недопустимый источник запроса"), None));
            }
        }
        if !header(req, "Content-Type")
            .unwrap_or("")
            .split(';')
            .next()
            .unwrap_or("")
            .trim()
            .eq_ignore_ascii_case("application/json")
        {
            return Ok((Reply::error(415, "Требуется application/json"), None));
        }
        let mut buf = Vec::new();
        req.as_reader()
            .take(sandbox::MAX_INPUT as u64 + 1)
            .read_to_end(&mut buf)?;
        if buf.len() > sandbox::MAX_INPUT {
            return Ok((Reply::error(413, "Запрос слишком большой"), None));
        }
        match serde_json::from_slice(&buf) {
            Ok(v) => v,
            Err(_) => return Ok((Reply::error(400, "Некорректный JSON"), None)),
        }
    } else {
        Value::Null
    };
    let _lock = store
        .writes
        .lock()
        .map_err(|_| io::Error::other("Хранилище недоступно"))?;
    let cookie = token_in(req);
    let session = match cookie.as_deref() {
        Some(key) => store.load(key)?,
        None => None,
    };
    let new = session.is_none();
    let revision = session.as_ref().map_or(0, |s| s.revision);
    let mut reply = sandbox::handle(sandbox::Request {
        method,
        target,
        revision,
        saved: session.map(|s| s.state),
        body,
    });
    let mut set_cookie = None;
    if let Some(state) = reply.saved.take() {
        let token = if new {
            new_token()?
        } else {
            cookie.expect("valid existing session")
        };
        store.save(
            &token,
            Session {
                revision: revision + 1,
                state,
            },
        )?;
        if new {
            set_cookie = Some(format!(
                "kpi_session={token}; Path=/; HttpOnly; SameSite=Strict"
            ))
        }
    }
    Ok((reply, set_cookie))
}
fn set_header(resp: &mut Response<std::io::Cursor<Vec<u8>>>, name: &str, value: &str) {
    if let Ok(h) = Header::from_bytes(name.as_bytes(), value.as_bytes()) {
        resp.add_header(h)
    }
}
fn mime(path: &Path) -> &'static str {
    match path.extension().and_then(|x| x.to_str()).unwrap_or("") {
        "html" => "text/html; charset=utf-8",
        "js" => "text/javascript; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "wasm" => "application/wasm",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "ico" => "image/x-icon",
        "woff2" => "font/woff2",
        "txt" => "text/plain; charset=utf-8",
        _ => "application/octet-stream",
    }
}
fn serve(mut req: Request, assets: &Path, store: &Store, port: u16) -> io::Result<()> {
    let path = req.url().split('?').next().unwrap_or("/").to_owned();
    let valid_host = header(&req, "Host").is_some_and(|host| {
        ["localhost", "127.0.0.1"]
            .iter()
            .any(|addr| host == format!("{addr}:{port}"))
    });
    if !valid_host {
        return req.respond(Response::empty(StatusCode(400)));
    }
    if path.starts_with("/api/") {
        let (r, cookie) = api(&mut req, store).unwrap_or_else(|_| {
            (
                Reply::error(500, "Не удалось прочитать или сохранить данные сеанса"),
                None,
            )
        });
        let bytes = if r.encoding == Some("base64") {
            STANDARD.decode(&r.body).map_err(io::Error::other)?
        } else {
            r.body.into_bytes()
        };
        let mut resp = Response::from_data(bytes).with_status_code(StatusCode(r.status));
        set_header(&mut resp, "Content-Type", r.content_type);
        set_header(&mut resp, "Cache-Control", "no-store");
        if r.content_type.starts_with("application/json") {
            set_header(&mut resp, "Vary", "Accept-Encoding");
        }
        set_header(&mut resp, "X-Content-Type-Options", "nosniff");
        if let Some(name) = r.filename {
            debug_assert!(name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b".-_".contains(&b)));
            set_header(
                &mut resp,
                "Content-Disposition",
                &format!("attachment; filename=\"{name}\""),
            );
        }
        if let Some(v) = cookie {
            set_header(&mut resp, "Set-Cookie", &v)
        }
        return req.respond(resp);
    }
    if req.method().as_str() != "GET" && req.method().as_str() != "HEAD" {
        return req.respond(Response::empty(StatusCode(405)));
    }
    let file = if path == "/" {
        "index.html"
    } else {
        path.strip_prefix('/').unwrap_or("")
    };
    if !file.split('/').all(|part| {
        !part.is_empty()
            && !part.starts_with('.')
            && part
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b".-_".contains(&b))
    }) || !Path::new(file)
        .components()
        .all(|c| matches!(c, Component::Normal(_)))
    {
        return req.respond(Response::empty(StatusCode(404)));
    }
    let file = assets.join(file);
    if !file
        .canonicalize()
        .ok()
        .is_some_and(|p| p.starts_with(assets))
    {
        return req.respond(Response::empty(StatusCode(404)));
    }
    let Ok(contents) = fs::read(&file) else {
        return req.respond(Response::empty(StatusCode(404)));
    };
    let mut resp = Response::from_data(contents);
    set_header(&mut resp, "Content-Type", mime(&file));
    set_header(&mut resp, "X-Content-Type-Options", "nosniff");
    req.respond(resp)
}
fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let mut args = std::env::args().skip(1);
    let mut assets = PathBuf::from("dist");
    let mut store = std::env::temp_dir().join("ttk-rust-dashboard-preview");
    let mut port = std::env::var("PORT")
        .unwrap_or_else(|_| "18765".into())
        .parse::<u16>()?;
    while let Some(key) = args.next() {
        let value = args.next().ok_or("Ожидалось значение аргумента")?;
        match key.as_str() {
            "--assets" => assets = value.into(),
            "--store" => store = value.into(),
            "--port" => port = value.parse()?,
            _ => return Err("Неизвестный аргумент".into()),
        }
    }
    if assets.join("client/index.html").is_file() {
        assets = assets.join("client")
    }
    let assets = Arc::new(assets.canonicalize()?);
    let store = Arc::new(Store::open(store)?);
    if store.path.canonicalize()?.starts_with(assets.as_path()) {
        return Err("Хранилище сеансов должно находиться вне web-каталога".into());
    }
    let server = Arc::new(Server::http(("127.0.0.1", port))?);
    let port = server.server_addr().to_ip().ok_or("TCP")?.port();
    println!("http://127.0.0.1:{port}");
    std::thread::scope(|scope| {
        for _ in 0..4 {
            let (server, store, assets) =
                (Arc::clone(&server), Arc::clone(&store), Arc::clone(&assets));
            scope.spawn(move || {
                for req in server.incoming_requests() {
                    let _ = serve(req, &assets, &store, port);
                }
            });
        }
    });
    Ok(())
}
