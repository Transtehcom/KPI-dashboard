"""Serve the original dashboard's HTTP handler with an in-memory fixture for replay."""
import datetime
import json
import os
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import threading
from types import SimpleNamespace


def prohibit_external_io(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "subprocess.Popen"}:
        raise PermissionError("External I/O is disabled in the replay oracle")
    if event == "sqlite3.connect" and args[0] != ":memory:":
        raise PermissionError("Only in-memory SQLite is allowed in the replay oracle")


def main():
    root = Path(__file__).resolve().parent
    sys.path.insert(0, str(root / "original"))
    sys.addaudithook(prohibit_external_io)
    from app import common, engine, server, supplier_directory, supply_documents

    data = json.loads((root / "fixture.json").read_text(encoding="utf-8"))
    snapshot_at = datetime.datetime.fromisoformat(data["snapshot_at"])

    class SnapshotTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return snapshot_at if tz is None else snapshot_at.astimezone(tz)

    supplier_directory.dt = SimpleNamespace(datetime=SnapshotTime, timedelta=datetime.timedelta)
    engine.today = lambda: snapshot_at.date()
    users = [
        ("a", "director", "manager", "Администрация"),
        ("b", "sales", "employee", "Отдел продаж"),
        ("c", "supply", "employee", "Отдел снабжения"),
        ("e", "logistics", "employee", "Отдел логистики"),
    ]
    # Deliberately skip App.__init__/serve: these are the original routing and
    # storage methods, without the production config loader or scheduled jobs.
    app = server.App.__new__(server.App)
    app.config = {"employees": [
        {"login": login, "name": login.title(), "role": role, "department": department,
         "position": {"director": None, "sales": "pm", "supply": "supply", "logistics": "logistics"}[login]}
        for _, login, role, department in users
    ]}
    app.lock = threading.RLock()
    app.con = common.connect(":memory:")
    con = app.con
    app.rules = common.load_rules()
    app.engine = engine.Engine(con, app.rules, app.config)
    supplier_directory.store(con, data["suppliers"], now=data["meta"]["synced_at"])
    con.execute("INSERT OR REPLACE INTO supplier_meta VALUES (?, ?)",
                ("directory_attempt_at", "2099-01-01T00:00:00"))
    for row in data["enrichment"]:
        con.execute(
            "INSERT INTO supplier_region_enrichment (supplier_key,region,checked_at) "
            "VALUES (:supplier_key,:region,:checked_at)", row)
    for row in data["forwarders"]:
        con.execute(
            "INSERT INTO forwarder_orders (id, carrier, carrier_key, doc, dt, created_at, status, route, deleted, carrier_card) "
            "VALUES (:id,:carrier,:carrier_key,:doc,:dt,:created_at,:status,:route,:deleted,:card)",
            {**row, "card": json.dumps(row["carrier_card"], ensure_ascii=False)})
        if not row["deleted"] and row.get("responsible") == "logistics":
            names = row["contract_files"]
            contract = int(any(supply_documents.CONTRACT_NAME.search(name) for name in names))
            con.execute("INSERT INTO forwarder_contracts VALUES (?,?,?,?,?,?,?)",
                        (row["id"], row["responsible"], row["dt"], row["doc"], row["carrier"],
                         json.dumps(names, ensure_ascii=False), contract))
    con.execute("INSERT INTO dataset_state VALUES (?,?,?,?)",
                ("forwarder_contracts", "2026-11-01", "2026-11-30", data["snapshot_at"]))
    con.executemany("INSERT INTO manual_scores VALUES(?,?,?,?,?,?,?)",
                    [(row["emp"], row["week"], row["criterion"], row["score"], "Учебная оценка",
                      "director", data["snapshot_at"]) for row in data["weekly_scores"]])
    con.executemany("INSERT INTO sessions VALUES (?,?,?)",
                    [(letter * 64, login, "2099-01-01") for letter, login, _, _ in users])
    con.execute("INSERT INTO department_section_access VALUES (?,?)",
                ("Отдел снабжения", json.dumps(["carriers", "suppliers"])))
    con.commit()

    class FixtureHandler(server.Handler):
        pass

    FixtureHandler.app = app
    # HTTPServer's default bind calls getfqdn, unnecessary for a loopback fixture.
    class LocalServer(ThreadingHTTPServer):
        def server_bind(self):
            import socketserver
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address

    with LocalServer(("127.0.0.1", int(os.environ["PORT"])), FixtureHandler) as http:
        http.serve_forever()


if __name__ == "__main__":
    main()
