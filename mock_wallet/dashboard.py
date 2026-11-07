"""Read-only wallet dashboard.

    uvicorn dashboard:app --port 8050      # run from inside mock_wallet/

GET /             HTML dashboard (auto-refreshes every 15s)
GET /api/status   current run as JSON
GET /api/runs     every run, for comparing lives
"""

from __future__ import annotations

import os
from decimal import Decimal
from html import escape

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from report import recent_events, runs_history, wallet_status
from wallet import MockWallet, WalletError

app = FastAPI(title="Mock Wallet")


def _db():
    return os.environ.get("MOCK_WALLET_DB")


def _plain(d: dict) -> dict:
    return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}


def _usd(x: Decimal) -> str:
    return f"${x:,.2f}"


def _status():
    w = MockWallet(_db(), create=False)
    try:
        return wallet_status(w)
    finally:
        w.close()


def render_dashboard(status: dict, history: list[dict], events: list[dict]) -> str:
    alive = status["status"] == "alive"
    cards = [
        ("Run", f"#{status['run_id']}"),
        ("Status", "ALIVE" if alive else "COOKED"),
        ("Start balance", _usd(status["start_balance"])),
        ("Current balance", _usd(status["balance"])),
        ("Vault", _usd(status["vault"])),
        ("Peak equity", _usd(status["peak_equity"])),
        ("Current drawdown", f"{status['drawdown']:.2%}"),
        ("Equity (cash + open)", _usd(status["equity"])),
        ("Open positions", f"{status['open_positions']} · {_usd(status['open_position_value'])}"),
    ]
    card_html = "".join(
        f'<div class="card{" status " + ("alive" if alive else "cooked") if k == "Status" else ""}">'
        f'<div class="k">{escape(k)}</div><div class="v">{escape(v)}</div></div>'
        for k, v in cards)
    banner = ""
    if not alive:
        banner = (f'<div class="banner"><strong>COOKED</strong> at '
                  f'{escape(status["cooked_at"] or "")}: {escape(status["cooked_reason"] or "")}'
                  f'<br>Trading is halted. Run <code>python reset.py</code> to start '
                  f'run #{status["run_id"] + 1}.</div>')
    rows = "".join(
        f'<tr class="{escape(h["status"])}"><td>#{h["run_id"]}</td><td>{escape(h["status"])}</td>'
        f'<td>{_usd(h["start_balance"])}</td><td>{_usd(h["balance"])}</td>'
        f'<td>{_usd(h["vault"])}</td><td>{_usd(h["peak_equity"])}</td>'
        f'<td>{h["max_drawdown"]:.2%}</td><td>{h["trades"]}</td><td>{h["failed_sells"]}</td>'
        f'<td>{_usd(h["costs"] + h["slippage"])}</td><td>{h["return"]:.2%}</td>'
        f'<td class="cause">{escape(h["cooked_reason"] or "")}</td></tr>'
        for h in reversed(history))
    ev = "".join(
        f'<li><time>{escape(e["ts"])}</time> <b>{escape(e["kind"])}</b> '
        f'{escape(e["message"])}</li>' for e in events)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>Mock Wallet</title>
<style>
:root {{ --bg:#f6f7f9; --fg:#14171c; --muted:#5d6673; --card:#fff; --line:#e1e4e8;
         --ok:#137333; --bad:#b3261e; --badbg:#fce8e6; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#0f1115; --fg:#e7e9ee; --muted:#9aa3b2; --card:#181b21; --line:#2a2f38;
           --ok:#6dd58c; --bad:#ff8a80; --badbg:#3a1d1b; }} }}
body {{ margin:0; padding:24px 16px; background:var(--bg); color:var(--fg);
       font:15px/1.45 system-ui, sans-serif; }}
main {{ max-width:1100px; margin:0 auto; }}
h1 {{ font-size:22px; margin:0 0 16px; }} h2 {{ font-size:16px; margin:28px 0 10px; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(170px, 1fr)); gap:10px; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }}
.k {{ color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
.v {{ font-size:20px; font-weight:600; font-variant-numeric:tabular-nums; margin-top:2px; }}
.status.alive .v {{ color:var(--ok); }} .status.cooked .v {{ color:var(--bad); }}
.banner {{ background:var(--badbg); color:var(--bad); border-radius:10px; padding:12px 14px;
           margin-bottom:14px; }}
.tablewrap {{ overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; background:var(--card); font-variant-numeric:tabular-nums; }}
th, td {{ text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); white-space:nowrap; }}
th {{ color:var(--muted); font-weight:500; font-size:12px; }}
tr.cooked td:nth-child(2) {{ color:var(--bad); }} tr.alive td:nth-child(2) {{ color:var(--ok); }}
td.cause {{ white-space:normal; color:var(--muted); min-width:220px; }}
ul {{ list-style:none; padding:0; margin:0; }} li {{ padding:6px 0; border-bottom:1px solid var(--line); }}
time {{ color:var(--muted); font-size:12px; margin-right:6px; }}
</style></head><body><main>
<h1>Mock Wallet: Run #{status['run_id']}</h1>
{banner}
<div class="grid">{card_html}</div>
<h2>Every run</h2>
<div class="tablewrap"><table><thead><tr><th>Run</th><th>Status</th><th>Start</th>
<th>Balance</th><th>Vault</th><th>Peak</th><th>Max DD</th><th>Trades</th><th>Failed sells</th>
<th>Costs</th><th>Return</th><th>Cause of death</th></tr></thead><tbody>{rows}</tbody></table></div>
<h2>Recent events</h2><ul>{ev or '<li>No events yet.</li>'}</ul>
</main></body></html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    try:
        status = _status()
    except WalletError:
        return HTMLResponse("<p>No runs yet. Run <code>python reset.py</code>.</p>")
    return render_dashboard(status, runs_history(_db()),
                            recent_events(_db(), limit=25, run_id=status["run_id"]))


@app.get("/api/status")
def api_status():
    try:
        return _plain(_status())
    except WalletError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@app.get("/api/runs")
def api_runs():
    return [_plain(h) for h in runs_history(_db())]
