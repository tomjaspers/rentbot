import time
from datetime import datetime
from pathlib import Path

from rentbot import main
from rentbot.models import Listing
from rentbot.schedule import TZ, next_slot
from rentbot.store import Store

CRITERIA = {"postcode_districts": ["BS3"], "min_beds": 2, "max_beds": 2, "max_price_pcm": 1750,
            "property_types": "any", "garden": "required", "alert_on_balcony": True}
CFG = {"criteria": CRITERIA, "council_tax": {"band_d_annual": 2713.68, "discount": 0.25},
       "sources": {"rightmove": {"enabled": True}, "onthemarket": {"enabled": True}}}


class Recorder:
    def __init__(self):
        self.group, self.admin, self.edits = [], [], []

    def send(self, text, reply_to=None):
        self.group.append(text)
        return len(self.group)

    def send_listing(self, listing, headline="New match"):
        return self.send(f"{headline}: {listing.address}")

    def update_listing(self, message_id, listing, also):
        self.edits.append((message_id, [s for s, _ in also]))

    def alert_admin(self, text, level="warn"):
        self.admin.append(text)


class NoHttp:
    def pause(self):
        pass


def fake_source(name, listings):
    class Fake:
        fail = False

        def __init__(self, cfg, criteria):
            pass

        def search(self, http):
            if Fake.fail:
                raise ConnectionError("blocked")
            return [Listing(**vars(l)) for l in listings]  # fresh copies, like a real fetch

        def enrich(self, http, listing):
            return listing
    Fake.name = name
    return Fake


def house(source, id, address="Exmoor Street, BS3", price=1550):
    return Listing(source, id, f"https://{source}/{id}", address, price, 2, description="Private garden")


def run(store, rec, sources):
    main.run_once(CFG | {"sources": {n: {"enabled": True} for n in sources}}, store, rec, http=NoHttp())


def test_group_only_gets_real_house_alerts(monkeypatch):
    rm_list, otm_list = [house("rightmove", "1")], []
    rm, otm = fake_source("rightmove", rm_list), fake_source("onthemarket", otm_list)
    monkeypatch.setattr(main, "SOURCES", {"rightmove": rm, "onthemarket": otm})
    store, rec = Store(Path(":memory:")), Recorder()

    run(store, rec, ["rightmove", "onthemarket"])  # first run: recorded quietly, summary to admin
    assert rec.group == [] and "first run" in rec.admin[0]

    rm_list.append(house("rightmove", "2", "Raymend Road, BS3", 1500))
    run(store, rec, ["rightmove", "onthemarket"])
    assert rec.group == ["New match: Raymend Road, BS3"]

    otm_list.append(house("onthemarket", "9", "Raymend Road, Bristol BS3", 1500))  # same house, other site
    run(store, rec, ["rightmove", "onthemarket"])
    assert len(rec.group) == 1 and rec.edits == [(1, ["onthemarket"])]

    rm_list[1].price_pcm = 1450  # real price drop -> group
    run(store, rec, ["rightmove", "onthemarket"])
    assert rec.group[-1] == "Price drop (was £1,500): Raymend Road, BS3"

    run(store, rec, ["rightmove", "onthemarket"])  # nothing new: nothing anywhere
    assert len(rec.group) == 2

    store.set_status(store.get("rightmove:2")["fingerprint"], "rejected")
    rm_list[1].price_pcm = 1400  # price drop on a rejected house: stays quiet and rejected
    run(store, rec, ["rightmove", "onthemarket"])
    assert len(rec.group) == 2 and store.get("rightmove:2")["status"] == "rejected"
    assert len(rec.admin) == 1  # only the first-run summary; no per-run logs


def test_two_machines_split_sources_without_rechecking_each_other(monkeypatch, capsys):
    rm, orent = fake_source("rightmove", [house("rightmove", "1")]), fake_source("openrent", [])
    monkeypatch.setattr(main, "SOURCES", {"rightmove": rm, "openrent": orent})
    store, rec = Store(Path(":memory:")), Recorder()
    cfg = CFG | {"sources": {"rightmove": {"enabled": True}, "openrent": {"enabled": False}}}

    main.run_once(cfg, store, rec, NoHttp(), runner="github")                    # seeds
    monkeypatch.setattr(main, "config_hash", lambda cfg: "laptop-code")         # laptop runs other code
    main.run_once(cfg, store, rec, NoHttp(), only=["openrent"], runner="laptop")
    monkeypatch.setattr(main, "config_hash", lambda cfg: "github-code")
    main.run_once(cfg, store, rec, NoHttp(), runner="github")                    # first re-check of rightmove
    capsys.readouterr()
    main.run_once(cfg, store, rec, NoHttp(), runner="github")
    monkeypatch.setattr(main, "config_hash", lambda cfg: "laptop-code")
    main.run_once(cfg, store, rec, NoHttp(), only=["openrent"], runner="laptop")
    assert "re-checking" not in capsys.readouterr().out  # no ping-pong between code versions
    runs = store.runs_since(0)
    assert [r["runner"] for r in runs] == ["github", "laptop", "github", "github", "laptop"]
    assert all(set(r["details"]) == ({"openrent"} if r["runner"] == "laptop" else {"rightmove"}) for r in runs)


def test_heartbeat_twice_a_day(monkeypatch):
    monkeypatch.setattr(main, "SOURCES", {"rightmove": fake_source("rightmove", [])})
    store, rec = Store(Path(":memory:")), Recorder()
    cfg = CFG | {"sources": {"rightmove": {"enabled": True}}}
    for _ in range(3):
        main.run_once(cfg, store, rec, http=NoHttp())
    t = time.time() + 3600  # the runs just now fall in the first heartbeat's 12 hours, not the second's
    hour = datetime.fromtimestamp(t, TZ).hour
    for now in (t, t + 600, t + 11 * 3600, t + 12 * 3600):
        main.maybe_send_heartbeat(store, rec, [hour, (hour + 12) % 24], now=now)
    beats = [a for a in rec.admin if a.startswith("heartbeat")]
    assert len(beats) == 2 and beats[0].startswith("heartbeat: 3 runs in the last 12h (3 local; 0 failed)")
    assert beats[1].startswith("heartbeat: 0 runs in the last 12h")
    assert rec.group == []


def test_schedule_slots():
    sched = [{"hours": list(range(7, 23)), "minutes": [0, 10, 20, 30, 40, 50]}, {"hours": [2, 4], "minutes": [0]}]
    at = lambda h, m: datetime(2026, 10, 23, h, m, tzinfo=TZ).timestamp()
    assert next_slot(sched, at(9, 3)) == at(9, 10)
    assert next_slot(sched, at(9, 10)) == at(9, 20)
    assert next_slot(sched, at(22, 50)) == datetime(2026, 10, 24, 2, 0, tzinfo=TZ).timestamp()
    assert next_slot(sched, at(4, 0)) == at(7, 0)
    weekend = [{"hours": [9], "minutes": [0, 30], "wdays": [1, 2, 3, 4, 5]},
               {"hours": [9, 10], "minutes": [0], "wdays": [0, 6]}]
    sat = lambda h, m: datetime(2026, 10, 24, h, m, tzinfo=TZ).timestamp()  # 2026-10-23 is a Friday
    assert next_slot(weekend, at(9, 0)) == at(9, 30)
    assert next_slot(weekend, sat(9, 0)) == sat(10, 0)
    assert next_slot(weekend, sat(10, 0)) == datetime(2026, 10, 25, 9, 0, tzinfo=TZ).timestamp()


def test_source_failure_goes_to_admin_once_then_recovery(monkeypatch):
    src = fake_source("rightmove", [])
    monkeypatch.setattr(main, "SOURCES", {"rightmove": src})
    store, rec = Store(Path(":memory:")), Recorder()
    src.fail = True
    run(store, rec, ["rightmove"])
    run(store, rec, ["rightmove"])
    assert sum("search failed" in a for a in rec.admin) == 1
    src.fail = False
    run(store, rec, ["rightmove"])
    assert "Rightmove is working again." in rec.admin
    assert rec.group == []
    assert store.last_run()["ok"] == 1
