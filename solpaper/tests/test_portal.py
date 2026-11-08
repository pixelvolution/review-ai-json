"""Portal: read-only data views, token-guarded buttons, local-only server, launcher."""

import http.client
import json
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

import launcher
from solpaper.config import load_config
from solpaper.db import connect
from solpaper.portal import data
from solpaper.portal.procs import ProcessManager
from solpaper.portal.server import Portal, serve
from solpaper.wallet import MockWallet, WalletConfig

SLEEPER = {"sleeper": ["-c", "import time\nwhile True: time.sleep(0.1)"]}


@pytest.fixture
def seeded(tmp_path):
    cfg = load_config()
    cfg["db_path"] = str(tmp_path / "portal.db")
    conn = connect(cfg["db_path"])
    conn.execute("INSERT INTO pairs (pair_address, dex, name, first_seen_at, first_source) "
                 "VALUES ('A', 'pumpswap', 'AAA / SOL', '2026-11-07T20:00:00.000+00:00', 'test')")
    conn.execute("INSERT INTO pairs_seen (pair_address, observed_at, source, price_usd, "
                 "liquidity_usd) VALUES ('A', '2026-11-07T20:00:05.000+00:00', 'ds_refresh', 1, 5e4)")
    w = MockWallet(cfg["db_path"], WalletConfig(**cfg["wallet"]))
    p = w.open_position("A", 1.0, 10)
    w.close_position(p["position_id"], 1.2)
    conn.execute("INSERT INTO trades (pair_address, name, status, notional, pnl, exit_fill_t, "
                 "exit_reason, wallet_run_id) VALUES ('A', 'AAA / SOL', 'closed', 10, 1.9, 1.78e9, "
                 "'take_profit', 1)")
    conn.commit()
    return cfg


@pytest.fixture
def running(seeded, tmp_path):
    procs = ProcessManager(tmp_path, tmp_path / "logs", commands=SLEEPER)
    portal = Portal(seeded, tmp_path, procs=procs)
    quit_called = threading.Event()
    portal.on_quit = quit_called.set
    httpd = serve(portal, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield portal, httpd.server_address[1], quit_called
    procs.stop_all()
    httpd.shutdown()
    httpd.server_close()


def call(port, path, method="GET", token=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"X-Portal-Token": token} if token else {}
    if host:
        headers["Host"] = host
    conn.request(method, path, headers=headers)
    r = conn.getresponse()
    body = r.read()
    conn.close()
    return r.status, (json.loads(body) if r.getheader("Content-Type", "").startswith(
        "application/json") else body.decode())


# ------------------------------------------------------------------ data views

def test_data_views_read_the_database(seeded):
    s = data.summary(seeded["db_path"])
    assert s["db"] and s["capture"]["pairs"] == 1 and s["capture"]["observations"] == 1
    assert s["wallet"]["run"] == 1 and s["wallet"]["balance"] > 100
    assert s["trades"]["closed"] == 1 and s["trades"]["wins"] == 1
    assert data.trades(seeded["db_path"])[0]["exit_reason"] == "take_profit"
    assert len(data.equity(seeded["db_path"])) >= 1


def test_data_views_are_empty_not_errors_without_a_database(tmp_path):
    db = str(tmp_path / "missing.db")
    assert data.summary(db) == {"db": False}
    assert data.positions(db) == data.trades(db) == data.signals(db) == data.equity(db) == []


def test_data_views_cannot_write(seeded):
    conn = data.open_db(seeded["db_path"])
    with pytest.raises(Exception):
        conn.execute("DELETE FROM trades")


# ------------------------------------------------------------------ server

def test_page_and_all_endpoint(running):
    portal, port, _ = running
    code, html = call(port, "/")
    assert code == 200 and portal.token in html and "__TOKEN__" not in html
    code, body = call(port, "/api/all")
    assert code == 200 and body["summary"]["trades"]["closed"] == 1
    assert set(body) == {"processes", "summary", "positions", "trades", "signals", "equity"}
    assert call(port, "/api/ping")[1]["app"] == "solpaper-portal"


def test_buttons_need_the_token(running):
    portal, port, _ = running
    assert call(port, "/api/start/sleeper", "POST")[0] == 403
    assert call(port, "/api/start/sleeper", "POST", token="wrong")[0] == 403
    assert not portal.procs.running("sleeper")


def test_other_hostnames_are_refused(running):
    portal, port, _ = running
    assert call(port, "/api/all", host="evil.example:80")[0] == 403
    assert call(port, "/api/start/sleeper", "POST", token=portal.token,
                host="evil.example")[0] == 403
    assert call(port, "/api/all", host=f"localhost:{port}")[0] == 200


def test_start_stop_logs_and_unknown_process(running):
    portal, port, _ = running
    code, st = call(port, "/api/start/sleeper", "POST", token=portal.token)
    assert code == 200 and st["running"] and st["pid"]
    assert call(port, "/api/all")[1]["processes"]["sleeper"]["running"]
    code, st = call(port, "/api/stop/sleeper", "POST", token=portal.token)
    assert code == 200 and not st["running"]
    assert "started" in "\n".join(call(port, "/api/logs?name=sleeper")[1]["lines"])
    assert call(port, "/api/start/nope", "POST", token=portal.token)[0] == 404
    assert call(port, "/api/logs?name=../../etc/passwd")[0] == 404


def test_export_writes_csvs(running):
    portal, port, _ = running
    code, body = call(port, "/api/export", "POST", token=portal.token)
    assert code == 200 and portal.exports.is_dir() and any(portal.exports.glob("*.csv"))


def test_quit_button_calls_shutdown(running):
    portal, port, quit_called = running
    assert call(port, "/api/quit", "POST")[0] == 403
    assert call(port, "/api/quit", "POST", token=portal.token)[0] == 200
    assert quit_called.wait(2)


def test_server_listens_on_loopback_only(seeded, tmp_path):
    httpd = serve(Portal(seeded, tmp_path, ProcessManager(tmp_path, tmp_path / "l")), 0)
    try:
        assert httpd.server_address[0] == "127.0.0.1"
    finally:
        httpd.server_close()


# ------------------------------------------------------------------ processes

def test_process_manager_stops_a_running_child(tmp_path):
    pm = ProcessManager(tmp_path, tmp_path / "logs", python=sys.executable, commands=SLEEPER)
    pm.start("sleeper")
    pid = pm.status()["sleeper"]["pid"]
    assert pm.start("sleeper")["pid"] == pid        # starting twice doesn't spawn a second one
    t = time.time()
    pm.stop_all()
    assert not pm.running("sleeper") and time.time() - t < 15


# ------------------------------------------------------------------ launcher

def test_launcher_reads_the_port_file_and_finds_no_portal(tmp_path):
    assert launcher.portal_url(tmp_path) == "http://127.0.0.1:8765/"
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "portal.port").write_text("8790")
    assert launcher.portal_url(tmp_path) == "http://127.0.0.1:8790/"
    assert launcher.venv_python(tmp_path) is None
    assert launcher.portal_up("http://127.0.0.1:9/") is False


def test_launcher_sees_a_running_portal(running):
    _, port, _ = running
    assert launcher.portal_up(f"http://127.0.0.1:{port}/")


# ------------------------------------------------------------------ update, restart, reset

def git(cwd, *args):
    import subprocess
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c",
                    "init.defaultBranch=main", *args], cwd=cwd, check=True, capture_output=True)


def test_update_pulls_new_commits_and_reports_them(seeded, tmp_path):
    origin, work, mine = tmp_path / "origin.git", tmp_path / "work", tmp_path / "mine"
    git(tmp_path, "init", "--bare", str(origin))
    git(tmp_path, "clone", str(origin), str(work))
    (work / "a.txt").write_text("1")
    git(work, "add", ".")
    git(work, "commit", "-m", "first")
    git(work, "push", "origin", "HEAD")
    git(tmp_path, "clone", str(origin), str(mine))
    portal = Portal(seeded, mine, ProcessManager(mine, tmp_path / "logs"))
    r = portal.update()
    assert r["ok"] and not r["changed"]
    (work / "a.txt").write_text("2")
    git(work, "commit", "-am", "second")
    git(work, "push", "origin", "HEAD")
    r = portal.update()
    assert r["ok"] and r["changed"] and r["commits"][0].endswith("second")
    assert (mine / "a.txt").read_text() == "2" and r["restart_needed"]
    # Pulled elsewhere (GitHub Desktop): nothing new, but the running code is old.
    (work / "a.txt").write_text("3")
    git(work, "commit", "-am", "third")
    git(work, "push", "origin", "HEAD")
    git(mine, "pull")
    r = portal.update()
    assert r["ok"] and not r["changed"] and r["restart_needed"]
    assert Portal(seeded, mine, portal.procs).update()["restart_needed"] is False


def test_update_outside_a_git_folder_says_so(seeded, tmp_path):
    r = Portal(seeded, tmp_path, ProcessManager(tmp_path, tmp_path / "logs")).update()
    assert r["ok"] is False and r["output"]


def test_restart_button_and_boot_id(running):
    portal, port, _ = running
    called = threading.Event()
    portal.on_restart = called.set
    assert call(port, "/api/ping")[1]["boot"] == portal.boot
    assert call(port, "/api/restart", "POST")[0] == 403
    assert call(port, "/api/restart", "POST", token=portal.token)[0] == 200
    assert called.wait(2)


def test_reset_starts_a_new_run_only_after_a_cook_and_restarts_the_scanner(seeded, tmp_path):
    procs = ProcessManager(tmp_path, tmp_path / "logs", commands={"scanner": SLEEPER["sleeper"]})
    portal = Portal(seeded, tmp_path, procs=procs)
    httpd = serve(portal, 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        code, body = call(port, "/api/reset", "POST", token=portal.token)
        assert code == 409 and "still alive" in body["error"]      # run 1 isn't cooked
        MockWallet(seeded["db_path"]).cook("test")
        procs.start("scanner")
        pid = procs.status()["scanner"]["pid"]
        code, body = call(port, "/api/reset", "POST", token=portal.token)
        assert code == 200 and body["run"] == 2 and body["scanner_restarted"]
        assert procs.running("scanner") and procs.status()["scanner"]["pid"] != pid
        assert call(port, "/api/all")[1]["summary"]["wallet"]["status"] == "alive"
    finally:
        procs.stop_all()
        httpd.shutdown()
        httpd.server_close()


def test_trade_summary_counts_only_the_current_run(seeded):
    MockWallet(seeded["db_path"]).cook("test")
    from solpaper.reset import reset, wallet_config
    assert data.summary(seeded["db_path"])["trades"]["total"] == 1
    reset(seeded["db_path"], wallet_config(seeded))
    t = data.summary(seeded["db_path"])["trades"]
    assert t["run"] == 2 and t["total"] == 0 and t["pnl"] == 0
    assert data.trades(seeded["db_path"])[0]["run"] == 1      # history still listed, with its run
