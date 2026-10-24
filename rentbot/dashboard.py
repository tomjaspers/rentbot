import gzip
import hashlib
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

RUN_COOLDOWN_SECONDS = 120  # the dashboard has no password, so don't let anyone spam runs
STATE_CACHE_SECONDS = 60    # scans are at most every 15 min, so a minute-old state is fine
HUNG_SCAN_SECONDS = 1800


class Activity:
    """What this process knows about scans, so the watchdog doesn't have to ask the database: polling
    it every few minutes would stop Neon's compute from ever scaling to zero."""

    def __init__(self):
        self.requested = time.time()        # last time a scan was asked for (cron-job.org or "Run now")
        self.started: float | None = None   # when the scan running in this process started


def watchdog(store: Store, notifier, schedule: list[dict], max_late_minutes: int, activity: Activity,
             check_every: int = 300) -> None:
    """Alerts the admin when scheduled runs stop happening or one hangs. The condition must hold on
    two checks in a row, so waking the laptop from sleep (launchd catches up within a minute) is quiet."""
    strikes, alerted = 0, False
    while True:
        time.sleep(check_every)
        now = time.time()
        problem = None
        if activity.started and now - activity.started > HUNG_SCAN_SECONDS:
            problem = f"a scan started {int((now - activity.started) / 60)} min ago and hasn't finished"
        elif now - next_slot(schedule, activity.requested) > max_late_minutes * 60:
            problem = database_problem(store, schedule, max_late_minutes, now)  # another machine may have scanned
        strikes = strikes + 1 if problem else 0
        if strikes >= 2 and not alerted:
            notifier.alert_admin(f"watchdog: {problem}")
            alerted = True
        elif not problem and alerted:
            notifier.alert_admin("watchdog: runs are happening again.")
            alerted = False


def database_problem(store: Store, schedule: list[dict], max_late_minutes: int, now: float) -> str | None:
    """Whether the run history shows runs stopping or hanging, whichever machine ran them."""
    try:
        run = store.last_run()
    except Exception as e:
        return f"dashboard can't read the database: {e!r}"
    if run is None:
        return "no runs recorded yet"
    if not run["finished"] and now - run["started"] > HUNG_SCAN_SECONDS:
        return f"run {run['id']} started {int((now - run['started']) / 60)} min ago and hasn't finished"
    due = next_slot(schedule, run["started"])
    if now - due > max_late_minutes * 60:
        return f"no run for {int((now - run['started']) / 60)} min (one was due {int((now - due) / 60)} min ago)"
    return None


def start_scan(cfg: dict, target, notifier, busy: threading.Lock, activity: Activity, on_done=None) -> bool:
    """Scans in a background thread so the HTTP request returns at once. False if one is already
    running in this process (other machines are kept out by the database lock)."""
    from rentbot.main import default_runner, scan_once

    activity.requested = time.time()
    if not busy.acquire(blocking=False):
        return False

    def work():
        activity.started = time.time()
        try:
            scan_once(cfg, target, notifier, default_runner())
        finally:
            activity.started = None
            busy.release()
            if on_done:
                on_done()

    threading.Thread(target=work, daemon=True).start()
    return True


def serve(store: Store, cfg: dict, notifier=None, host: str | None = None, port: int | None = None,
          scan_target=None) -> None:
    # Render sets PORT and needs 0.0.0.0; locally stay on localhost.
    host = host or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
    port = port or int(os.environ.get("PORT", 8765))
    last_trigger = [0.0]
    scanning = threading.Lock()
    activity = Activity()
    scan_token = os.environ.get("SCAN_TOKEN", "")
    # (expires, body, etag): every byte read from Neon counts against its monthly transfer allowance.
    cached = [(0.0, b"", "")]

    def state() -> tuple[bytes, str]:
        expires, body, etag = cached[0]
        if time.time() > expires:
            now = time.time()
            nxt = next_slot(cfg["schedule"], now)
            s = store.dashboard_state() | {"next_run": nxt, "criteria": cfg["criteria"], "walk_to": cfg.get("walk_to")}
            body = json.dumps(s).encode()
            etag = f'"{hashlib.sha1(body).hexdigest()[:16]}"'
            cached[0] = (min(now + STATE_CACHE_SECONDS, nxt), body, etag)  # refresh once next_run has passed
        return body, etag

    def invalidate() -> None:
        cached[0] = (0.0, b"", "")

    def scan() -> bool:
        return start_scan(cfg, scan_target, notifier, scanning, activity, on_done=invalidate)

    if notifier is not None:
        threading.Thread(target=watchdog, daemon=True,
                         args=(store, notifier, cfg["schedule"], cfg.get("watchdog_minutes", 45), activity)).start()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str = "application/json", etag: str = "") -> None:
            """With an ETag the browser revalidates instead of downloading again (304 when unchanged)."""
            if etag and self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            zipped = len(body) > 1024 and "gzip" in self.headers.get("Accept-Encoding", "")
            if zipped:
                body = gzip.compress(body)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-cache" if etag else "no-store")
            if etag:
                self.send_header("ETag", etag)
            if zipped:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj).encode())

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                page = PAGE.read_bytes()
                self._send(200, page, "text/html; charset=utf-8", f'"{hashlib.sha1(page).hexdigest()[:16]}"')
            elif self.path == "/healthz":  # Render health checks and wake-ups: never touches the database
                self._json(200, {"ok": True})
            elif self.path == "/api/state":
                body, etag = state()
                self._send(200, body, etag=etag)
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path == "/api/status":
                try:
                    n = store.set_status(body["fingerprint"], body["status"])
                except (KeyError, ValueError) as e:
                    return self._json(400, {"error": repr(e)})
                invalidate()
                self._json(200, {"updated": n})
            elif self.path == "/api/run":
                wait = RUN_COOLDOWN_SECONDS - (time.time() - last_trigger[0])
                if wait > 0:
                    return self._json(429, {"ok": False, "error": f"a run was just started; try again in {int(wait)}s"})
                if not scan():
                    return self._json(429, {"ok": False, "error": "a run is in progress"})
                last_trigger[0] = time.time()
                self._json(200, {"ok": True, "error": ""})
            elif self.path == "/api/scan":  # cron-job.org calls this on the schedule in config.yaml
                auth = self.headers.get("Authorization", "")
                if not scan_token or not hmac.compare_digest(auth, f"Bearer {scan_token}"):
                    return self._json(401, {"error": "unauthorized"})
                started = scan()
                self._json(202 if started else 409, {"started": started})
            else:
                self._json(404, {"error": "not found"})

        def log_message(self, *args):
            pass

    print(f"dashboard on http://{host}:{port}", flush=True)
    ThreadingHTTPServer((host, port), Handler).serve_forever()
