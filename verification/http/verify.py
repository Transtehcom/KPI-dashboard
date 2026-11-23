"""Record the Python HTTP oracle and replay the same requests against the native Rust server."""
import argparse
from contextlib import contextmanager
from http.client import HTTPConnection
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode
from zipfile import ZipFile

HERE = Path(__file__).resolve().parent
SITE = HERE.parent.parent
REFERENCE = "7d2da43eb827317cb86f99482ee2e7c79f95ed40"


def cookie(letter):
    return {"Cookie": "kpi_session=" + letter * 64} if letter else {}


def cases():
    for obj in ["carriers", "supplier-directory"]:
        path = "/api/" + obj
        for letter, label in [("", "anonymous"), ("d", "unknown-session"), ("b", "denied"),
                              ("a", "admin"), ("c", "permitted-employee")]:
            yield {"name": f"{obj}-{label}", "path": path, "headers": cookie(letter)}
        for letter, label in [("", "anonymous"), ("b", "denied")]:
            yield {"name": f"{obj}-xlsx-{label}", "path": path + ".xlsx", "headers": cookie(letter)}
    for i, term in enumerate(["опора", "Опора металло", "металло опора", "пермь", "КРЕПЕЖ",
                              "0000000102", "неизвестный", "+", " "], 1):
        yield {"name": f"supplier-search-{i:02d}", "path": "/api/supplier-directory?" +
               urlencode({"search": term}), "headers": cookie("a")}
    yield {"name": "carrier-json-search-is-unfiltered", "path": "/api/carriers?search=none",
           "headers": cookie("a")}
    base = {"emp": "logistics", "month": "2026-11", "code": "logistics_documents",
            "kind": "forwarder_order", "ref": "demo-f-0-1"}
    for route, good, bad in [("kpi-decision", 1, True), ("kpi-exclusion", True, 1)]:
        values = [("anonymous", "", base | {"value": good}), ("staff", "b", base | {"value": good}),
                  ("01-valid", "a", base | {"value": good}),
                  ("02-clear", "a", base | {"value": None if route == "kpi-decision" else False}),
                  ("bad-value", "a", base | {"value": bad}), ("missing-value", "a", base),
                  ("bad-month", "a", base | {"value": good, "month": "2026-00"}),
                  ("wrong-employee", "a", base | {"value": good, "emp": "missing"}),
                  ("other-month", "a", base | {"value": good, "month": "2026-10"}),
                  ("wrong-kind", "a", base | {"value": good, "kind": "meeting"}),
                  ("wrong-ref", "a", base | {"value": good, "ref": "missing"})]
        if route == "kpi-decision":
            values += [("00-reject", "a", base | {"value": 0})]
        week = base | {"code": "logistics_cargo_safety", "kind": "cargo_safety_week",
                       "ref": "2026-11-02"}
        values += [("week-01-set", "a", week | {"value": good}),
                   ("week-02-reset", "a", week | {"value": None if route == "kpi-decision" else False}),
                   ("wrong-week", "a", week | {"ref": "2026-11-03", "value": good}),
                   ("unscored-week", "a", week | {"ref": "2026-11-09", "value": good})]
        for label, letter, body in values:
            yield {"name": f"{route}-{label}", "method": "POST", "path": "/api/" + route,
                   "headers": cookie(letter) | {"Content-Type": "application/json"},
                   "body": json.dumps(body, ensure_ascii=False)}


def make_lab(checkout, location):
    location.mkdir(parents=True, exist_ok=True)
    lab = Path(tempfile.mkdtemp(prefix="http-", dir=location))
    result = subprocess.run(
        ["git", "-C", str(checkout), "archive", "--format=zip", REFERENCE, "app", "kpi_rules.json"],
        check=True, stdout=subprocess.PIPE)
    with ZipFile(BytesIO(result.stdout)) as archive:
        for entry in archive.namelist():
            path = Path(entry)
            if (path.parts[0] == "app" and path.suffix in {".py", ".json"} and ".." not in path.parts
                    or entry == "kpi_rules.json"):
                archive.extract(entry, lab / "original")
    shutil.copyfile(HERE / "original.py", lab / "oracle.py")
    shutil.copyfile(SITE / "src/http_contract/directory-fixture.json", lab / "fixture.json")
    (lab / "inputs.jsonl").write_text(
        "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases()), encoding="utf-8")
    (lab / "store").mkdir()
    for letter, role, person in [("a", "admin", "director"), ("b", "employee", "sales"),
                                  ("c", "employee", "supply")]:
        state = {"role": role, "person": person, "values": {"viewer": person}}
        if person == "supply":
            for section in ["suppliers", "carriers"]:
                state["values"]["access/Отдел снабжения/" + section] = "1"
        (lab / "store" / (letter * 64 + ".json")).write_text(
            json.dumps({"state": json.dumps(state, ensure_ascii=False), "revision": 1},
                       ensure_ascii=False), encoding="utf-8")
    (lab / "assets").mkdir()
    (lab / "assets/index.html").write_text("<!doctype html><title>Fixture</title>", encoding="ascii")
    return lab


def rust_anything(tool, lab, name, args, allow_mismatch=False):
    run = subprocess.run([str(tool), "app", *args], cwd=lab, stdout=subprocess.PIPE)
    (lab / (name + ".json")).write_bytes(run.stdout)
    if run.returncode and not (allow_mismatch and run.stdout):
        raise RuntimeError(f"{name} failed ({run.returncode}). Reports are in {lab}")
    return json.loads(run.stdout)


@contextmanager
def local_server(command, lab):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen(command, cwd=lab, env={**os.environ, "PORT": str(port)})
    try:
        deadline = time.monotonic() + 30
        while proc.poll() is None:
            try:
                probe = socket.create_connection(("127.0.0.1", port), timeout=.3)
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(.05)
            else:
                probe.close()
                yield port
                return
        raise RuntimeError(f"Server did not listen on loopback ({proc.poll()})")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def fetch(port, path, letter):
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request("GET", path, headers=cookie(letter))
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def post(port, path, payload, session_cookie=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        headers = {"Content-Type": "application/json"}
        if session_cookie:
            headers["Cookie"] = session_cookie
        conn.request("POST", path, json.dumps(payload, ensure_ascii=False).encode(), headers)
        response = conn.getresponse()
        return response.status, response.getheader("Set-Cookie"), json.loads(response.read())
    finally:
        conn.close()


def check_sessions_and_restart(native, lab):
    start = "/api/sandbox/session"
    action = "/api/sandbox/action"
    with local_server(native, lab) as port:
        status, set_cookie, view = post(port, start, {"route": "home", "time": "2026-11-23T07:00:00Z"})
        assert status == 200 and set_cookie and "HttpOnly" in set_cookie and "SameSite=Strict" in set_cookie
        assert "saved" not in view and "state" not in view and "html" in view and view["route"] == "home"
        cookie_a = set_cookie.split(";", 1)[0]
        first_revision = view["revision"]
        body = {"key": "theme", "value": "", "revision": first_revision}
        status, _, view = post(port, action, body, cookie_a)
        assert status == 200 and view["theme"] == "dark" and view["revision"] > first_revision
        assert post(port, action, body, cookie_a)[0] == 409
        status, session_b, view = post(port, start, {"route": "home"})
        assert status == 200 and session_b.split(";", 1)[0] != cookie_a and view["theme"] == "light"
    with local_server(native, lab) as port:
        status, new_cookie, view = post(port, start, {"route": "home"}, cookie_a)
        assert status == 200 and not new_cookie and view["theme"] == "dark"
        assert view["revision"] > first_revision + 1 and "saved" not in view and "state" not in view
        assert post(port, start, {"route": "home"}, session_b.split(";", 1)[0])[2]["theme"] == "light"
    (lab / "session-check.json").write_text(
        json.dumps({"opaque_cookie": True, "state_stays_on_server": True, "stale_write_status": 409,
                    "separate_browser_isolation": True, "restored_after_restart": True}), encoding="utf-8")


def xlsx(port, path):
    from openpyxl import load_workbook
    status, headers, body = fetch(port, path, "a")
    headers = {k.lower(): v for k, v in headers.items()}
    assert status == 200 and "spreadsheetml" in headers.get("content-type", ""), (path, status)
    book = load_workbook(BytesIO(body))
    try:
        sheets = []
        for ws in book:
            cells = [[(c.value, c.data_type if c.value is not None else None) for c in row]
                     for row in ws.iter_rows()]
            sheets.append((ws.title, str(ws.freeze_panes), ws.auto_filter.ref, cells))
        return headers.get("content-disposition"), sheets
    finally:
        book.close()


def meaningful_results(port):
    for path, key, count in [("carriers", "rows", 3), ("supplier-directory", "list", 6)]:
        for letter, status in [("", 401), ("d", 401), ("b", 403), ("c", 200), ("a", 200)]:
            code, _, body = fetch(port, "/api/" + path, letter)
            assert code == status, (path, letter, code, status)
            if code == 200:
                assert len(json.loads(body)[key]) == count, path
    _, _, body = fetch(port, "/api/supplier-directory?search=0000000102", "a")
    matches = json.loads(body)["list"]
    assert len(matches) == 1 and matches[0]["fields"]["Реквизит: ИНН"] == "0000000102"
    kpi = {"emp": "logistics", "month": "2026-11", "code": "logistics_documents",
           "kind": "forwarder_order", "ref": "demo-f-0-1"}
    for route, values in [("kpi-decision", [0, 1, None]), ("kpi-exclusion", [True, False])]:
        for value in values:
            status, _, result = post(port, "/api/" + route, kpi | {"value": value}, cookie("a")["Cookie"])
            assert status == 200 and result == {"ok": True}, (route, value, status, result)
        week = kpi | {"code": "logistics_cargo_safety", "kind": "cargo_safety_week",
                      "ref": "2026-11-09", "value": values[0]}
        assert post(port, "/api/" + route, week, cookie("a")["Cookie"])[0] == 400


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, help="Original dashboard Git checkout")
    parser.add_argument("--tool", type=Path, required=True, help="rust-anything 3.3.0 executable")
    parser.add_argument("--output", type=Path, default=Path(tempfile.gettempdir()))
    parser.add_argument("--lab", type=Path, help="Reuse the recording at this path")
    parser.add_argument("--rust-server", type=Path, help="Compiled native ttk-preview executable")
    opts = parser.parse_args()
    tool = opts.tool.resolve()
    if opts.lab:
        lab = opts.lab.resolve(strict=True)
    elif opts.reference:
        lab = make_lab(opts.reference.resolve(), opts.output.resolve())
    else:
        parser.error("One of --reference / --lab is required")
    print("Laboratory:", lab, flush=True)
    original = [sys.executable, "-E", "oracle.py"]
    if not opts.lab:
        with local_server(original, lab) as port:
            meaningful_results(port)
        result = rust_anything(tool, lab, "record", [
            "record", ".", "--http", "--no-traps", "--normalize", "key-order",
            "--inputs", "inputs.jsonl", "--json", "--", *original])
        if result["noisy"] or result["unstable"] or result["timed_out"] or result["cases"] != len(list(cases())):
            raise AssertionError(result)
        print("Stable HTTP cases:", result["cases"])
        rust_anything(tool, lab, "original-replay", ["replay", ".", "--json"])
    if opts.rust_server:
        binary = lab / ("ttk-preview" + opts.rust_server.suffix)
        shutil.copy2(opts.rust_server, binary)
        native = [binary.as_posix(), "--assets", "assets", "--store", "store"]
        replay = rust_anything(tool, lab, "rust-replay", [
            "replay", ".", "--build", "./" + binary.name + " --assets assets --store store", "--json"],
            allow_mismatch=True)
        print(f"Native HTTP replay: {replay['passed']}/{replay['total']}", flush=True)
        paths = ["/api/carriers.xlsx", "/api/supplier-directory.xlsx",
                 "/api/carriers.xlsx?" + urlencode({"search": "Казань"}),
                 "/api/supplier-directory.xlsx?search=0000000102",
                 "/api/carriers.xlsx?search=unknown", "/api/supplier-directory.xlsx?search=unknown"]
        with local_server(original, lab) as port:
            baseline = [xlsx(port, path) for path in paths]
        compared = []
        filenames = []
        with local_server(native, lab) as port:
            meaningful_results(port)
            for path, reference in zip(paths, baseline):
                got_name, got_sheets = xlsx(port, path)
                filenames.append((path, reference[0], got_name))
                assert len(got_sheets) == len(reference[1]), (path, "Sheet count")
                for wanted, got in zip(reference[1], got_sheets):
                    assert wanted[:3] == got[:3], (path, "Sheet name, panes, filter", wanted[:3], got[:3])
                    assert len(wanted[3]) == len(got[3]), (path, "Row count")
                    for i, (left, right) in enumerate(zip(wanted[3], got[3]), 1):
                        assert len(left) == len(right), (path, "Column count", i)
                        for j, (x, y) in enumerate(zip(left, right), 1):
                            assert x == y, (path, "Cell", i, j, x, y)
                compared.append({"path": path, "sheets": [
                    {"name": title, "rows": len(cells), "columns": len(cells[0]),
                     "frozen": frozen, "autofilter": filt}
                    for title, frozen, filt, cells in reference[1]]})
        for path, wanted_name, got_name in filenames:
            assert wanted_name == got_name, (path, "Content-Disposition", wanted_name, got_name)
        (lab / "workbooks.json").write_text(json.dumps(compared, ensure_ascii=False, indent=2), encoding="utf-8")
        check_sessions_and_restart(native, lab)
        if replay["passed"] != len(list(cases())) or replay["passed"] != replay["total"] or replay["skipped"]:
            raise AssertionError(f"HTTP replay differs: see {lab / 'rust-replay.json'}")
        print(f"Rust passed HTTP replay and all {len(paths)} workbook comparisons.")


if __name__ == "__main__":
    main()
