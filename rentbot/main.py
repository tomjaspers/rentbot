import argparse
import hashlib
import html
import json
import os
import time
import traceback
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

from rentbot import enrich
from rentbot.dedupe import fingerprint
from rentbot.filters import basic_match, full_match
from rentbot.http import Http
from rentbot.models import Listing
from rentbot.notify import SOURCE_NAMES, Telegram, format_listing
from rentbot.sources.onthemarket import OnTheMarket
from rentbot.sources.openrent import OpenRent
from rentbot.sources.rightmove import Rightmove
from rentbot.store import Store

ROOT = Path(__file__).resolve().parent.parent
SOURCES = {"rightmove": Rightmove, "onthemarket": OnTheMarket, "openrent": OpenRent}


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


class ConsoleNotifier:
    """Stands in for Telegram during --dry-run."""

    def __init__(self):
        self.n = 0

    def send(self, text: str, reply_to: int | None = None) -> int:
        self.n += 1
        print(f"\n--- message {self.n}{f' (reply to {reply_to})' if reply_to else ''} ---\n{text}")
        return self.n

    def send_listing(self, listing: Listing, headline: str = "New match") -> int:
        return self.send(format_listing(listing, headline))

    def update_listing(self, message_id: int, listing: Listing, also: list) -> None:
        print(f"\n--- edit message {message_id} ---\n{format_listing(listing, 'New match', also)}")

    def alert_admin(self, text: str, level: str = "warn") -> None:
        print(f"\n--- to admin ({level}) ---\n{text}")


def load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


# Code whose changes alter how a listing is parsed or judged; editing any of it triggers a quiet re-check.
LOGIC_FILES = ["enrich.py", "filters.py", "dedupe.py", "sources/*.py"]


def config_hash(cfg: dict) -> str:
    h = hashlib.sha1(json.dumps({k: cfg.get(k) for k in ("criteria", "walk_to", "council_tax")},
                                sort_keys=True).encode())
    pkg = Path(__file__).parent
    for f in sorted(p for pattern in LOGIC_FILES for p in pkg.glob(pattern)):
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


def run_once(cfg: dict, store: Store, notifier, http: Http) -> None:
    criteria = cfg["criteria"]
    chash = config_hash(cfg)
    seeding = store.is_empty()
    # After a config change every listing is re-checked; new matches then go out as one summary.
    rechecking = not seeding and store.get_meta("config_hash") != chash
    if seeding:
        log("empty database: recording current listings without alerting")
    elif rechecking:
        log("config/code changed: re-checking every listing quietly")
    quiet_matches: dict[str, Listing] = {}
    pending: list[tuple[Listing, str, str]] = []
    run_id = store.start_run()
    details: dict[str, dict] = {}

    for name, cls in SOURCES.items():
        scfg = cfg["sources"].get(name) or {}
        if not scfg.get("enabled"):
            continue
        source = cls(scfg, criteria)
        stats = details[name] = {"results": 0, "new": 0, "alerts": 0, "error": None, "detail_errors": 0}
        try:
            results = source.search(http)
        except Exception as e:
            log(f"{name}: search failed: {e!r}")
            stats["error"] = repr(e)[:300]
            status = store.record_failure(name, repr(e))
            if not status["warned"]:  # once per outage; a recovery message follows
                notifier.alert_admin(f"{SOURCE_NAMES[name]} search failed: {e!r}")
                store.mark_warned(name)
            continue
        if (store.get_source_status(name) or {}).get("warned"):
            notifier.alert_admin(f"{SOURCE_NAMES[name]} is working again.")
        store.record_success(name)
        stats["results"] = len(results)

        for listing in results:
            fp = fingerprint(listing)
            row = store.get(listing.key)
            headline = "New match"
            dropped = bool(row and row["price_pcm"] and listing.price_pcm and listing.price_pcm < row["price_pcm"])
            if row and not dropped and row["criteria_hash"] == chash:
                store.touch(listing.key)
                continue
            if dropped:
                headline = f"Price drop (was £{row['price_pcm']:,})"
            stats["new"] += 1

            ok, why = basic_match(listing, criteria)
            if ok:
                try:
                    http.pause()
                    source.enrich(http, listing)
                except Exception as e:
                    log(f"{name}: detail fetch failed for {listing.url}: {e!r}")
                    stats["detail_errors"] += 1
                    stats.setdefault("detail_error_examples", []).append(f"{listing.url}: {e!r}"[:200])
                    continue  # not stored, so it is retried next run
                enrich.apply(listing, cfg)
                ok, why = full_match(listing, criteria)
                fp = fingerprint(listing)  # the detail page may have filled in the address
            if not ok:
                store.save(listing, fp, False, why, criteria_hash=chash)
                continue

            # The group only ever hears about a house once (plus real price drops).
            first_alert = store.alerted_with_fingerprint(fp)
            if seeding or store.status_for_fingerprint(fp) == "rejected" or (first_alert and not dropped) \
                    or (row and row["matched"] and not dropped):
                if seeding:
                    quiet_matches.setdefault(fp, listing)
                elif first_alert and not row and first_alert["source"] != name:
                    add_site_to_alert(store, notifier, first_alert, listing)
                store.save(listing, fp, True, criteria_hash=chash)
                continue
            if rechecking:  # newly matching after a config/code change: sent together below
                pending.append((listing, fp, headline))
                store.save(listing, fp, True, criteria_hash=chash)
                continue

            msg_id = notifier.send_listing(listing, headline)
            log(f"{name}: ALERT {listing.address} £{listing.price_pcm} score={listing.score}")
            stats["alerts"] += 1
            store.save(listing, fp, True, message_id=msg_id, criteria_hash=chash)
        log(f"{name}: {len(results)} results, {stats['new']} new/changed")

    if pending:
        send_pending(store, notifier, pending, chash)
    if seeding:
        ranked = sorted(quiet_matches.values(), key=lambda l: -l.score)
        notifier.alert_admin(
            f"first run: recorded {len(ranked)} current matches without alerting the group:\n" + "\n".join(
                f"• {l.score} · £{l.price_pcm:,} {l.address}" for l in ranked), level="info")

    detail_failures = {n: d for n, d in details.items() if d["detail_errors"]}
    if detail_failures:
        notifier.alert_admin("Some listing pages failed to load (they'll be retried next run):\n" + "\n".join(
            f"{SOURCE_NAMES[n]}: {d['detail_errors']}× e.g. {d['detail_error_examples'][0]}"
            for n, d in detail_failures.items()))

    ok = all(not d["error"] for d in details.values())
    if ok:
        store.set_meta("config_hash", chash)
    store.finish_run(run_id, ok, details)

    if any(d["new"] or d["alerts"] or d["error"] for d in details.values()) or rechecking:
        notifier.alert_admin(f"run {run_id}{' (re-check after config/code change)' if rechecking else ''}:\n" + "\n".join(
            f"{SOURCE_NAMES[n]}: " + (f"error {d['error']}" if d["error"] else
                                      f"{d['results']} results, {d['new']} new/changed, {d['alerts']} alerts")
            for n, d in details.items()) + (f"\n{len(pending)} newly matching sent to the group" if pending else ""),
            level="info")


# A re-check that suddenly matches more than this sends one digest instead of separate alerts.
MAX_INDIVIDUAL_ALERTS = 5


def send_pending(store: Store, notifier, pending: list[tuple[Listing, str, str]], chash: str) -> None:
    unique = list({fp: (l, fp, h) for l, fp, h in pending}.values())
    if len(unique) <= MAX_INDIVIDUAL_ALERTS:
        for listing, fp, headline in unique:
            msg_id = notifier.send_listing(listing, headline)
            store.save(listing, fp, True, message_id=msg_id, criteria_hash=chash)
        return
    ranked = sorted(unique, key=lambda t: -t[0].score)
    msg_id = notifier.send(f"<b>{len(ranked)} more houses now match:</b>\n" + "\n".join(
        f'• score {l.score} · £{l.price_pcm:,} · {html.escape(l.address)} <a href="{html.escape(l.url)}">link</a>'
        for l, _, _ in ranked))
    for listing, fp, _ in ranked:
        store.save(listing, fp, True, message_id=msg_id, criteria_hash=chash)


def add_site_to_alert(store: Store, notifier, first_alert: dict, listing: Listing) -> None:
    """Same house turned up on another site: edit the original group message to add the link."""
    if store.fingerprints_for_message(first_alert["message_id"]) > 1:
        return  # announced in a digest message; editing it per house would garble it
    try:
        original = Listing(**json.loads(first_alert["data"]))
        others = [(r["source"], r["url"]) for r in store.rows_for_fingerprint(first_alert["fingerprint"])
                  if r["key"] != first_alert["key"]]
        others.append((listing.source, listing.url))
        notifier.update_listing(first_alert["message_id"], original, also=others)
    except Exception as e:  # e.g. digest messages, or Telegram refusing an edit
        notifier.alert_admin(f"couldn't add {SOURCE_NAMES[listing.source]} link to the alert for "
                             f"{listing.address}: {e!r}", level="info")


def db_target(sqlite_path: Path) -> str | Path:
    """Neon Postgres when DATABASE_URL is set (production), else the local SQLite file."""
    return os.environ.get("DATABASE_URL") or sqlite_path


def cli() -> None:
    p = argparse.ArgumentParser(prog="rentbot")
    p.add_argument("command", choices=["run", "loop", "test-alert", "dashboard"])
    p.add_argument("--dry-run", action="store_true", help="print alerts instead of sending; use a throwaway database")
    p.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    p.add_argument("--db", type=Path, default=ROOT / "seen.db")
    args = p.parse_args()

    load_dotenv(ROOT / ".env")
    cfg = load_config(args.config)
    notifier = ConsoleNotifier() if args.dry_run else Telegram()

    if args.command == "dashboard":
        from rentbot.dashboard import serve
        serve(Store(db_target(args.db)), cfg, notifier)
        return

    if args.command == "test-alert":
        notifier.alert_admin("test: logs and failure alerts reach you here.", level="info")
        return  # never posts to the group: it only gets real house alerts

    store = None
    while True:
        try:
            if store is None:
                store = Store(Path(":memory:") if args.dry_run else db_target(args.db))
                if args.dry_run:  # skip seeding so every match prints as an alert
                    store.save(Listing("dry", "run", "", "", None, None), "", False)
                http = Http(cfg["http"]["timeout_seconds"], tuple(cfg["http"]["detail_delay_seconds"]))
            run_once(cfg, store, notifier, http)
        except Exception:
            tb = traceback.format_exc()
            log("run failed:\n" + tb)
            notifier.alert_admin(f"run crashed:\n{tb[-1500:]}")
        if args.command != "loop":
            break
        time.sleep(cfg.get("loop_interval_seconds", 600))
    if store is not None:
        store.db.close()


if __name__ == "__main__":
    cli()
