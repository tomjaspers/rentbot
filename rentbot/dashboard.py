import json
import os
import subprocess
import threading
import time

import httpx
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from rentbot.store import Store

PAGE = Path(__file__).with_name("dashboard.html")
LAUNCHD_LABEL = "com.tjaspers.rentbot"


def watchdog(store: Store, notifier, interval: int, max_gap_minutes: int, check_every: int = 300) -> None:
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
            elif now - run["started"] > max_gap_minutes * 60:
                problem = f"no run for {int((now - run['started']) / 60)} min (expected every {interval // 60})"
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


def trigger_run() -> tuple[bool, str]:
    """Starts a scan: via GitHub Actions in production (GITHUB_TOKEN + GITHUB_REPO set), else the
    local launchd job."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPO")
    if token and repo:
        r = httpx.post(f"https://api.github.com/repos/{repo}/actions/workflows/scan.yml/dispatches",
                       headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                       json={"ref": "main"}, timeout=20)
        return r.status_code == 204, "" if r.status_code == 204 else f"GitHub {r.status_code}: {r.text[:200]}"
    r = subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"],
                       capture_output=True, text=True)
    return r.returncode == 0, r.stderr


def serve(store: Store, cfg: dict, notifier=None, host: str | None = None, port: int | None = None) -> None:
    # Render sets PORT and needs 0.0.0.0; locally stay on localhost.
    host = host or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    port = port or int(os.environ.get("PORT", 8765))
    interval = cfg.get("loop_interval_seconds", 600)
    last_trigger = [0.0]
    if notifier is not None:
        threading.Thread(target=watchdog, args=(store, notifier, interval, cfg.get("watchdog_minutes", 60)),
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
                state["interval_seconds"] = interval
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
                ok, err = trigger_run()
                if ok:
                    last_trigger[0] = time.time()
                self._json(200 if ok else 500, {"ok": ok, "error": err})
            else:
                self._json(404, {"error": "not found"})

        def log_message(self, *args):
            pass

    print(f"dashboard on http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
