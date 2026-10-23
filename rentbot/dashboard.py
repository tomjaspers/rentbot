import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from rentbot.schedule import next_slot
from rentbot.store import Store

PAGE = Path(__file__).with_name("dashboard.html")


def watchdog(store: Store, notifier, schedule: list[dict], max_late_minutes: int, check_every: int = 300) -> None:
    """Alerts the admin when scheduled runs stop happening or one hangs. The condition must hold on
    two checks in a row, so waking the laptop from sleep (launchd catches up within a minute) is quiet."""
    strikes, alerted = 0, False
    while True:
        time.sleep(check_every)
        try:
            run = store.last_run()
            now = time.time()
            problem = None
            if run is None:
                problem = "no runs recorded yet"
            elif not run["finished"] and now - run["started"] > 1800:
                problem = f"run {run['id']} started {int((now - run['started']) / 60)} min ago and hasn't finished"
            elif now - next_slot(schedule, run["started"]) > max_late_minutes * 60:
                due = next_slot(schedule, run["started"])
                problem = (f"no run for {int((now - run['started']) / 60)} min "
                           f"(one was due {int((now - due) / 60)} min ago)")
        except Exception as e:
            problem = f"dashboard can't read the database: {e!r}"
        strikes = strikes + 1 if problem else 0
        if strikes >= 2 and not alerted:
            notifier.alert_admin(f"watchdog: {problem}")
            alerted = True
        elif not problem and alerted:
            notifier.alert_admin("watchdog: runs are happening again.")
            alerted = False


RUN_COOLDOWN_SECONDS = 120  # the dashboard has no password, so don't let anyone spam runs


def start_scan(cfg: dict, target, notifier, busy: threading.Lock) -> bool:
    """Scans in a background thread so the HTTP request returns at once. False if one is already
    running in this process (other machines are kept out by the database lock)."""
    from rentbot.main import default_runner, scan_once

    if not busy.acquire(blocking=False):
        return False

    def work():
        try:
            scan_once(cfg, target, notifier, default_runner())
        finally:
            busy.release()

    threading.Thread(target=work, daemon=True).start()
    return True


def serve(store: Store, cfg: dict, notifier=None, host: str | None = None, port: int | None = None,
          scan_target=None) -> None:
    # Render sets PORT and needs 0.0.0.0; locally stay on localhost.
    host = host or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    port = port or int(os.environ.get("PORT", 8765))
    last_trigger = [0.0]
    scanning = threading.Lock()
    scan_token = os.environ.get("SCAN_TOKEN", "")
    if notifier is not None:
        threading.Thread(target=watchdog, args=(store, notifier, cfg["schedule"], cfg.get("watchdog_minutes", 45)),
                         daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode())

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                state = store.dashboard_state()
                state["next_run"] = next_slot(cfg["schedule"], time.time())
                state["criteria"] = cfg["criteria"]
                state["walk_to"] = cfg.get("walk_to")
                self._json(200, state)
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path == "/api/status":
                try:
                    n = store.set_status(body["fingerprint"], body["status"])
                except (KeyError, ValueError) as e:
                    return self._json(400, {"error": repr(e)})
                self._json(200, {"updated": n})
            elif self.path == "/api/run":
                wait = RUN_COOLDOWN_SECONDS - (time.time() - last_trigger[0])
                if wait > 0:
                    return self._json(429, {"ok": False, "error": f"a run was just started; try again in {int(wait)}s"})
                if not start_scan(cfg, scan_target, notifier, scanning):
                    return self._json(429, {"ok": False, "error": "a run is in progress"})
                last_trigger[0] = time.time()
                self._json(200, {"ok": True, "error": ""})
            elif self.path == "/api/scan":  # the schedule: cron-job.org calls this every 10 min
                auth = self.headers.get("Authorization", "")
                if not scan_token or not hmac.compare_digest(auth, f"Bearer {scan_token}"):
                    return self._json(401, {"error": "unauthorized"})
                started = start_scan(cfg, scan_target, notifier, scanning)
                self._json(202 if started else 409, {"started": started})
            else:
                self._json(404, {"error": "not found"})

        def log_message(self, *args):
            pass

    print(f"dashboard on http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
