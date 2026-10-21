from pathlib import Path

from rentbot import main
from rentbot.models import Listing
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
