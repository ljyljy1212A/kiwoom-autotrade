"""Local paper-trading controller for the static dashboard.

Run from the repository root with ``python dashboard/dashboard_server.py``.
The controller intentionally refuses live accounts unless explicitly enabled
with ALLOW_LIVE_DASHBOARD=true.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlparse

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _load_dotenv() -> None:
    """Load the small subset of .env needed before the child program starts."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        # A newly edited project .env must replace values inherited by an old
        # PowerShell/dashboard process after credentials are rotated.
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


_load_dotenv()
PORT = int(os.environ.get("DASHBOARD_PORT", "8765"))
TRADE_HISTORY_LIMIT = max(1, int(os.environ.get("DASHBOARD_TRADE_HISTORY_LIMIT", "1000")))
TRADE_HISTORY_DAYS = max(1, int(os.environ.get("DASHBOARD_TRADE_HISTORY_DAYS", "365")))
_trade_history_cache: dict[str, tuple[tuple[int, int, int, int], dict]] = {}
_trade_history_cache_lock = Lock()
_json_snapshot_cache: dict[Path, tuple[tuple[int, int], dict]] = {}
_json_snapshot_cache_lock = Lock()


def _supervisor(action: str, account: str, market: str) -> tuple[int, dict]:
    """Ask the sole worker supervisor for lifecycle actions/status.

    A graceful stop can take the supervisor's 10-second graceful window plus
    its bounded forceful fallback.  The old eight-second dashboard timeout
    interrupted that request and left the browser with no response.
    """
    timeout = 20 if action == "stop" else 8
    command = [sys.executable, "-m", "src.worker_supervisor", action,
               "--account", account, "--market", market]
    try:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 4, {
            "account": account, "market": market, "running": True,
            "stopped": False, "reason": "dashboard-supervisor-timeout",
        }
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        payload = {"account": account, "running": False, "error": result.stderr.strip() or "Supervisor response invalid"}
    return result.returncode, payload


def _account_catalog() -> list[dict]:
    """Public account metadata only; secrets never leave the server process."""
    try:
        raw = yaml.safe_load((ROOT / "config" / "accounts.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    return [{
        "id": str(item.get("id", "")),
        "displayName": str(item.get("display_name", item.get("id", ""))),
        "market": str(item.get("market", "")).upper(),
        "mode": str(item.get("mode", "mock")).lower(),
    } for item in raw.get("accounts", []) if item.get("id")]


def _default_accounts() -> list[str]:
    configured = [item.strip() for item in os.environ.get("ACCOUNT_FILTER", "").split(",") if item.strip()]
    if configured:
        return configured
    # Safe controller default: do not silently start both markets.  The US
    # engine remains an explicit selection in the controller, while a normal
    # local dashboard launch opens the domestic mock account.
    known = {item["id"] for item in _account_catalog()}
    if "kr_mock" in known:
        return ["kr_mock"]
    return [item["id"] for item in _account_catalog() if item["mode"] == "mock"][:1]


def _active_markets() -> list[str]:
    accounts = _account_catalog()
    markets = []
    for market in ("KR", "US"):
        if any(_supervisor("status", item["id"], market)[1].get("running")
               for item in accounts if item["market"] == market):
            markets.append(market)
    return markets


def _active_accounts() -> list[str]:
    return [item["id"] for item in _account_catalog()
            if _supervisor("status", item["id"], item["market"])[1].get("running")]


def _worker_statuses() -> list[dict]:
    return [payload for item in _account_catalog()
            for _code, payload in [_supervisor("status", item["id"], item["market"])]
            if payload.get("running")]


def _open_lifecycle_starts(path: Path) -> dict[str, str]:
    """Return active symbol lifecycle boundaries for current-holding summaries.

    Ledger history is intentionally retained for execution history and P&L.
    Per-tranche holdings, however, must never combine a closed cycle with a
    new manual T1 in the same symbol.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(symbol).upper().lstrip("A"): str(state["started_at"])
        for symbol, state in payload.items()
        if isinstance(state, dict) and state.get("status") == "open" and state.get("started_at")
    }


def _overlay_authoritative_tranche_metadata(account: str, snapshot: dict, data_dir: Path | None = None) -> dict:
    """Keep dashboard basis metadata aligned with lifecycle/cache state.

    A passive balance snapshot can outlive the active strategy that wrote it.
    Broker moving averages in that snapshot must not replace a recorded manual
    Line 1 basis in the dashboard.
    """
    data_dir = data_dir or (ROOT / "data")
    result = dict(snapshot)
    try:
        bases = json.loads((data_dir / f"tranche_bases_{account}.json").read_text(encoding="utf-8"))
        if isinstance(bases, dict):
            result["trancheBases"] = bases
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    try:
        lifecycles = json.loads((data_dir / f"symbol_lifecycles_{account}.json").read_text(encoding="utf-8"))
        if isinstance(lifecycles, dict):
            open_rows = [(str(symbol).upper().lstrip("A"), row) for symbol, row in lifecycles.items()
                         if isinstance(row, dict) and row.get("status") == "open"]
            result["manualTrancheQty"] = {symbol: float(row.get("manual_qty", 0) or 0)
                                           for symbol, row in open_rows}
            result["manualTrancheBases"] = {symbol: float(row.get("manual_price", 0) or 0)
                                             for symbol, row in open_rows
                                             if float(row.get("manual_price", 0) or 0) > 0}
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return result


def _trade_history_payload(
    account: str, db_path: Path, lifecycle_starts: dict[str, str] | None = None,
) -> dict:
    """Read a bounded local ledger slice, cached until SQLite changes.

    This is dashboard-only SQLite I/O. It never calls a broker API and runs in
    the threaded dashboard server process, independently from trading workers.
    """
    stat = db_path.stat()
    wal_path = Path(f"{db_path}-wal")
    try:
        wal = wal_path.stat()
        fingerprint = (stat.st_mtime_ns, stat.st_size, wal.st_mtime_ns, wal.st_size)
    except OSError:
        fingerprint = (stat.st_mtime_ns, stat.st_size, 0, 0)
    with _trade_history_cache_lock:
        cached = _trade_history_cache.get(account)
        if cached and cached[0] == fingerprint:
            return cached[1]
    cutoff = (datetime.now(timezone.utc) - timedelta(days=TRADE_HISTORY_DAYS)).isoformat()
    # The dashboard never writes to the ledger.  On Windows, SQLite's default
    # read/write open may still try to acquire a journal/locking sidecar even
    # after ``query_only`` is set, which makes a read-only dashboard process
    # fail with "unable to open database file".  Open read-only, but do not
    # use SQLite's ``immutable=1`` hint: immutable connections ignore the WAL
    # sidecar and can therefore miss a just-confirmed fill while the worker is
    # still running.  WAL readers are safe here and see every committed
    # ledger transition without receiving write permission.
    db_uri = f"{db_path.resolve().as_uri()}?mode=ro"
    if lifecycle_starts is None:
        lifecycle_starts = _open_lifecycle_starts(ROOT / "data" / f"symbol_lifecycles_{account}.json")
    lifecycle_terms = " OR ".join("(b.symbol=? AND b.created_at>=?)" for _ in lifecycle_starts)
    lifecycle_filter = f" AND ({lifecycle_terms})" if lifecycle_terms else " AND 1=0"
    lifecycle_args = [value for item in lifecycle_starts.items() for value in item]
    db = sqlite3.connect(db_uri, uri=True, timeout=0.25)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        rows = db.execute("""
            WITH recent AS (
                SELECT id, symbol, type, step, filled_at AS filledAt, qty, price,
                       buy_id AS buyId, created_at AS createdAt
                FROM trade_ledger WHERE account_id=? AND created_at>=?
            ), linked_buys AS (
                SELECT b.id, b.symbol, b.type, b.step, b.filled_at AS filledAt, b.qty, b.price,
                       b.buy_id AS buyId, b.created_at AS createdAt
                FROM trade_ledger b JOIN recent s ON s.buyId=b.id
                WHERE b.account_id=?
            )
            SELECT * FROM recent UNION SELECT * FROM linked_buys
            ORDER BY createdAt DESC, id DESC LIMIT ?
        """, (account, cutoff, account, TRADE_HISTORY_LIMIT)).fetchall()
        summaries = db.execute("""
            WITH open_buy_lots AS (
                SELECT b.symbol, b.step, b.price,
                       b.qty - COALESCE(SUM(s.qty), 0) AS openQty
                FROM trade_ledger AS b
                LEFT JOIN trade_ledger AS s
                  ON s.account_id=b.account_id
                 AND s.type='sell'
                 AND s.buy_id=b.id
                WHERE b.account_id=? AND b.type='buy'""" + lifecycle_filter + """
                GROUP BY b.id, b.symbol, b.step, b.price, b.qty
            )
            SELECT symbol, step, SUM(openQty) AS qty,
                   SUM(openQty * price) / NULLIF(SUM(openQty), 0) AS avgPrice
            FROM open_buy_lots
            WHERE openQty > 0
            GROUP BY symbol, step
            ORDER BY symbol, step
        """, (account, *lifecycle_args)).fetchall()
    finally:
        # sqlite3's connection context manager commits/rolls back but does
        # not close the handle. Explicitly close it so the dashboard does not
        # retain a Windows handle to the live worker's database.
        db.close()
    payload = {
        "trades": [dict(row) for row in rows], "tranches": [dict(row) for row in summaries],
        "historyDays": TRADE_HISTORY_DAYS, "historyLimit": TRADE_HISTORY_LIMIT,
    }
    with _trade_history_cache_lock:
        _trade_history_cache[account] = (fingerprint, payload)
    return payload


def _json_snapshot(path: Path) -> dict:
    """Read a small engine snapshot safely, reusing it until its file changes."""
    stat = path.stat()
    fingerprint = (stat.st_mtime_ns, stat.st_size)
    with _json_snapshot_cache_lock:
        cached = _json_snapshot_cache.get(path)
        if cached and cached[0] == fingerprint:
            return cached[1]
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Snapshot payload must be an object")
    with _json_snapshot_cache_lock:
        _json_snapshot_cache[path] = (fingerprint, payload)
    return payload


def _account_market(account_id: str) -> str:
    return next((item["market"] for item in _account_catalog() if item["id"] == account_id), "")


def _validate_market_config(account_id: str, config: object) -> None:
    if not isinstance(config, dict):
        return
    expected = _account_market(account_id)
    actual = str(config.get("market", "")).upper()
    if not actual or actual != expected:
        raise ValueError(f"Profile market {actual or 'missing'} does not match {expected} account")


class Handler(BaseHTTPRequestHandler):
    def _path_and_query(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urlparse(self.path)
        return parsed.path, parse_qs(parsed.query)

    def _account(self, query: dict[str, list[str]]) -> str:
        requested = (query.get("account") or [""])[0]
        known = {item["id"] for item in _account_catalog()}
        if requested and requested in known:
            return requested
        return _default_accounts()[0] if _default_accounts() else "kr_mock"

    def _reject_real_account(self, account: str) -> bool:
        catalog = {item["id"]: item for item in _account_catalog()}
        if catalog.get(account, {}).get("mode") == "real" and os.environ.get("ALLOW_LIVE_DASHBOARD", "false").lower() != "true":
            self._json({"error": "Live accounts are disabled by the dashboard"}, 403)
            return True
        return False

    def _json(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path, query = self._path_and_query()
        if path == "/api/accounts":
            self._json({"accounts": _account_catalog(), "defaultAccounts": _default_accounts()})
            return
        if path == "/api/status":
            workers = _worker_statuses()
            self._json({"running": bool(workers), "accounts": [row["account"] for row in workers],
                        "markets": sorted({row.get("market") for row in workers if row.get("market")}),
                        "workers": workers})
            return
        if path == "/api/events":
            # This is a local event stream, not a polling endpoint. Workers
            # atomically publish one small file after a confirmed BUY/SELL fill;
            # the connected dashboard is released only when that event changes.
            account = self._account(query)
            if self._reject_real_account(account):
                return
            event_path = ROOT / "data" / f"dashboard_event_{account}.json"
            last_id = self.headers.get("Last-Event-ID", "")
            deadline = time.monotonic() + 25.0
            payload = None
            while time.monotonic() < deadline:
                try:
                    candidate = _json_snapshot(event_path)
                    event_id = str(candidate.get("id", ""))
                    if event_id and event_id != last_id:
                        payload = candidate
                        break
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
                time.sleep(0.2)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            if payload is not None:
                body = json.dumps(payload, separators=(",", ":"))
                event_type = str(payload.get("type") or "fill")
                self.wfile.write(f"id: {payload['id']}\nevent: {event_type}\ndata: {body}\n\n".encode())
            else:
                self.wfile.write(b": keepalive\n\n")
            self.wfile.flush()
            return
        if path == "/api/balance":
            account = self._account(query)
            if self._reject_real_account(account):
                return
            state_file = ROOT / "data" / f"balance_{account}.json"
            if not state_file.exists():
                self._json({"error": "No broker balance has been synchronized yet"}, 404)
                return
            try:
                self._json(_overlay_authoritative_tranche_metadata(account, _json_snapshot(state_file)))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self._json({"error": f"Balance snapshot is temporarily unavailable: {exc}"}, 503)
            return
        if path == "/api/trades":
            account = self._account(query)
            if self._reject_real_account(account):
                return
            db_path = ROOT / "data" / f"trades_{account}.db"
            if not db_path.exists():
                self._json({"trades": [], "tranches": [], "historyDays": TRADE_HISTORY_DAYS,
                            "historyLimit": TRADE_HISTORY_LIMIT})
                return
            try:
                self._json(_trade_history_payload(account, db_path))
            except (sqlite3.Error, OSError) as exc:
                self._json({"error": f"Trade history is temporarily unavailable: {exc}"}, 503)
            return
        if path == "/api/settings":
            account = self._account(query)
            if self._reject_real_account(account):
                return
            path = ROOT / "data" / f"dashboard_settings_{account}.json"
            if path.exists():
                try:
                    self._json(json.loads(path.read_text(encoding="utf-8")))
                    return
                except (OSError, json.JSONDecodeError):
                    pass
            self._json({"profiles": []})
            return
        if path in ("/", "/index.html"):
            page = (ROOT / "dashboard" / "control.html").read_bytes()
            content_type = "text/html; charset=utf-8"
        elif path == "/dashboard/index.html":
            page = (ROOT / "dashboard" / "index.html").read_bytes()
            content_type = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        if page:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            # The dashboard is edited locally while its server remains running.
            # Never serve an older cached script after a restart/refresh.
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return

    def do_POST(self):  # noqa: N802
        path, query = self._path_and_query()
        if path == "/api/settings":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                profiles = payload.get("profiles")
                if not isinstance(profiles, list):
                    raise ValueError("profiles must be a list")
                account = self._account(query)
                if self._reject_real_account(account):
                    return
                for profile in profiles:
                    if not isinstance(profile, dict):
                        raise ValueError("profile must be an object")
                    _validate_market_config(account, profile.get("config"))
                selected_id = str(payload.get("selected_profile_id", ""))
                settings_path = ROOT / "data" / f"dashboard_settings_{account}.json"
                try:
                    existing = json.loads(settings_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    existing = {}
                remove_closed = payload.get(
                    "auto_remove_closed_positions",
                    existing.get("auto_remove_closed_positions", True),
                )
                if not isinstance(remove_closed, bool):
                    raise ValueError("auto_remove_closed_positions must be a boolean")
                (ROOT / "data").mkdir(exist_ok=True)
                settings_path.write_text(
                    json.dumps({
                        "profiles": profiles,
                        "auto_remove_closed_positions": remove_closed,
                    }, ensure_ascii=False), encoding="utf-8"
                )
                # Every symbol owns a separate control file. This lets one
                # market worker run BIVI, BQ, and other enabled symbols at
                # the same time without replacing a global active symbol.
                for profile in profiles:
                    config = profile.get("config") if isinstance(profile, dict) else None
                    if not isinstance(config, dict) or not config.get("symbol"):
                        continue
                    symbol = str(config["symbol"]).upper()
                    enabled = profile.get("enabled", True) is not False
                    symbol_control = {
                        "symbol": symbol,
                        "auto_buy": enabled and bool((config.get("auto_buy") or {}).get("enabled")),
                        "auto_sell": enabled and bool((config.get("auto_sell") or {}).get("enabled")),
                        "config": config,
                    }
                    (ROOT / "data" / f"dashboard_control_{account}_{symbol}.json").write_text(
                        json.dumps(symbol_control, ensure_ascii=False), encoding="utf-8"
                    )
                # Saving a profile is itself an explicit activation action. This
                # removes the former requirement to click the list row before
                # Auto Buy/Sell could reach the running engine.
                active = next((p for p in profiles if str(p.get("id", "")) == selected_id), None)
                config = active.get("config") if isinstance(active, dict) else None
                if isinstance(config, dict) and config.get("symbol"):
                    control = {
                        "symbol": str(config["symbol"]).upper(),
                        "auto_buy": bool(config.get("auto_buy", {}).get("enabled", False)),
                        "auto_sell": bool(config.get("auto_sell", {}).get("enabled", False)),
                        "config": config,
                    }
                    (ROOT / "data" / f"dashboard_control_{account}.json").write_text(
                        json.dumps(control, ensure_ascii=False), encoding="utf-8"
                    )
                    (ROOT / "data" / f"dashboard_control_{account}_{control['symbol']}.json").write_text(
                        json.dumps(control, ensure_ascii=False), encoding="utf-8"
                    )
                else:
                    # A manual profile deletion must also revoke the prior
                    # runtime selection; otherwise the engine sees a stale
                    # symbol and emits an avoidable allow-list warning.
                    (ROOT / "data" / f"dashboard_control_{account}.json").write_text(
                        json.dumps({"symbol": "", "auto_buy": False, "auto_sell": False}),
                        encoding="utf-8",
                    )
                self._json({"profiles": profiles, "auto_remove_closed_positions": remove_closed})
            except (ValueError, json.JSONDecodeError):
                self._json({"error": "Invalid settings payload"}, 400)
            return
        if path == "/api/control":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                account = self._account(query)
                if self._reject_real_account(account):
                    return
                _validate_market_config(account, payload.get("config"))
                control = {
                    "symbol": str(payload.get("symbol", "")).upper(),
                    "auto_buy": bool(payload.get("auto_buy", False)),
                    "auto_sell": bool(payload.get("auto_sell", False)),
                    "config": payload.get("config") if isinstance(payload.get("config"), dict) else None,
                }
                (ROOT / "data").mkdir(exist_ok=True)
                (ROOT / "data" / f"dashboard_control_{account}.json").write_text(
                    json.dumps(control), encoding="utf-8"
                )
                if control["symbol"]:
                    (ROOT / "data" / f"dashboard_control_{account}_{control['symbol']}.json").write_text(
                        json.dumps(control), encoding="utf-8"
                    )
                self._json(control)
            except (ValueError, json.JSONDecodeError):
                self._json({"error": "Invalid control payload"}, 400)
            return
        if path == "/api/start":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._json({"error": "Invalid start payload"}, 400)
                return
            requested = payload.get("accounts", _default_accounts())
            if not isinstance(requested, list) or not requested:
                self._json({"error": "Select at least one account"}, 400)
                return
            catalog = {item["id"]: item for item in _account_catalog()}
            accounts = [str(item) for item in requested if str(item) in catalog]
            if not accounts:
                self._json({"error": "No valid account was selected"}, 400)
                return
            if any(catalog[item]["mode"] == "real" for item in accounts) and os.environ.get("ALLOW_LIVE_DASHBOARD", "false").lower() != "true":
                self._json({"error": "Live accounts are disabled by the dashboard"}, 403)
                return
            accounts_by_market: dict[str, list[str]] = {}
            for account in accounts:
                accounts_by_market.setdefault(catalog[account]["market"], []).append(account)
            launch_results = []
            for market, market_accounts in accounts_by_market.items():
                if len(market_accounts) != 1:
                    self._json({"error": f"{market} requires exactly one account per worker"}, 400)
                    return
                code, result = _supervisor("start", market_accounts[0], market)
                if code not in (0, 3):
                    self._json({"error": "Worker supervisor could not start the worker", "detail": result}, 503)
                    return
                launch_results.append({"market": market, **result})
            # A second start request is a refusal, not a successful second
            # launch.  Return the existing worker PID so the caller can make
            # that distinction without consulting dashboard-local state.
            self._json({
                "running": True,
                "accounts": _active_accounts(),
                "markets": _active_markets(),
                "launches": launch_results,
            })
            return
        if path == "/api/stop":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._json({"error": "Invalid stop payload"}, 400)
                return
            requested = payload.get("accounts", _active_accounts())
            if not isinstance(requested, list) or not requested:
                self._json({"error": "Select at least one running account to stop"}, 400)
                return
            catalog = {item["id"]: item for item in _account_catalog()}
            accounts = [str(item) for item in requested if str(item) in catalog]
            if not accounts:
                self._json({"error": "No valid account was selected"}, 400)
                return
            if any(catalog[item]["mode"] == "real" for item in accounts) and os.environ.get("ALLOW_LIVE_DASHBOARD", "false").lower() != "true":
                self._json({"error": "Live accounts are disabled by the dashboard"}, 403)
                return
            stops = []
            for account in accounts:
                item = catalog[account]
                code, result = _supervisor("stop", account, item["market"])
                stops.append({"account": account, "code": code, **result})
            running = _active_accounts()
            failed = [item for item in stops if not item.get("stopped")]
            self._json({"running": bool(running), "accounts": running, "stops": stops},
                       503 if failed else 200)
            return
        self.send_error(404)

    def log_message(self, *_args):
        return


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


if __name__ == "__main__":
    print(f"Dashboard: http://127.0.0.1:{PORT}")
    ReusableThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
