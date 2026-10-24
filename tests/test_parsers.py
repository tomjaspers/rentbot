import json
from pathlib import Path

import pytest

from rentbot.dedupe import fingerprint, street
from rentbot.filters import basic_match, classify_garden, full_match
from rentbot.models import Listing
from rentbot.sources import bristolreslet, onthemarket, openrent, rightmove
from rentbot.sources.base import parse_price_pcm

FIX = Path(__file__).parent / "fixtures"
CRITERIA = {"postcode_districts": ["BS3"], "min_beds": 2, "max_beds": 2, "max_price_pcm": 1600,
            "property_types": "any", "garden": "required", "alert_on_balcony": True}


def read(name):
    if not (FIX / name).exists():
        pytest.skip("site page fixtures are local only (not in the public repo)")
    return (FIX / name).read_text(encoding="utf-8")


def test_rightmove_search_and_detail():
    results = rightmove.parse_search(read("rightmove_search.html"))
    assert len(results) == 17
    exmoor = next(l for l in results if "Exmoor" in l.address)
    assert (exmoor.price_pcm, exmoor.beds, exmoor.property_type) == (1550, 2, "Terraced")
    assert exmoor.image.startswith("https://")
    rightmove.parse_detail(read("rightmove_detail.html"), exmoor)
    assert "Private Garden" in exmoor.features
    assert exmoor.description.startswith("Ocean are delighted")
    assert full_match(exmoor, CRITERIA) == (True, "")
    assert exmoor.garden == "yes"


def test_onthemarket_search_and_detail():
    results = onthemarket.parse_search(read("otm_search.html"))
    assert len({l.id for l in results}) == len(results)
    exmoor = next(l for l in results if "Exmoor" in l.address)
    assert (exmoor.price_pcm, exmoor.beds) == (1550, 2)
    onthemarket.parse_detail(read("otm_detail.html"), exmoor)
    assert "Private Garden" in exmoor.features
    assert classify_garden(exmoor)[0] == "yes"


def test_openrent_search_and_detail():
    results = openrent.parse_search(read("openrent_search.html"))
    assert len(results) > 10 and all(l.price_pcm and l.beds for l in results)
    first = results[0]
    assert first.id == "3066858" and first.price_pcm == 1200 and first.beds == 1
    assert "BS3" in first.address
    openrent.parse_detail(read("openrent_detail.html"), first)
    assert first.address == "1 Bed Flat, Skypark Road, BS3"
    assert first.property_type == "flat"
    assert first.garden_flag is False


def test_bristolreslet_search_and_detail():
    results = bristolreslet.parse_search(json.loads(read("bristolreslet_search.json")))
    assert results and all(l.price_pcm and l.beds for l in results)
    aubrey = next(l for l in results if "Aubrey" in l.address)
    assert (aubrey.price_pcm, aubrey.beds, aubrey.available) == (1950, 3, "2026-12-11")
    assert aubrey.url.endswith("/a1463377-6a7d-416f-8e8e-49bd6bff4505")
    bristolreslet.parse_detail(read("bristolreslet_detail.html"), aubrey)
    assert "Free on street parking" in aubrey.features
    assert (aubrey.garden_flag, aubrey.parking_types, aubrey.epc, aubrey.council_tax_band) == (True, ["on_street"], "D", "B")
    assert aubrey.description.startswith("Situated in the sought after Chessels area")


def test_same_house_on_two_sites_has_same_fingerprint():
    rm = Listing("rightmove", "1", "", "Exmoor Street - Southville", 1550, 2)
    otm = Listing("onthemarket", "2", "", "Exmoor Street, Bristol BS3", 1550, 2)
    assert street(rm.address) == "exmoor street"
    assert fingerprint(rm) == fingerprint(otm)


@pytest.mark.parametrize("text,expected", [
    ("Private Garden", "yes"),
    ("lovely south facing rear garden", "yes"),
    ("Roof terrace with views", "yes"),
    ("communal gardens", "maybe"),
    ("Juliet balcony", "maybe"),
    ("No garden. Terraced house.", "no"),
    ("Close to shops", "no"),
])
def test_classify_garden(text, expected):
    assert classify_garden(Listing("x", "1", "", "Sidmouth Gardens", 1500, 2, description=text))[0] == expected


@pytest.mark.parametrize("text,pcm", [("£1,550 pcm (£358 pw)", 1550), ("£350 pw", 1517), ("POA", None)])
def test_parse_price(text, pcm):
    assert parse_price_pcm(text) == pcm


def test_basic_match_rejects_other_district_and_price():
    assert basic_match(Listing("x", "1", "", "Ashton Rise, BS5", 1500, 2), CRITERIA)[0] is False
    assert basic_match(Listing("x", "1", "", "Paxton Drive, BS3", 1650, 2), CRITERIA)[0] is False
    assert basic_match(Listing("x", "1", "", "Raymend Road", 1400, 2), CRITERIA)[0] is True


def test_rightmove_detail_structured_fields():
    l = next(x for x in rightmove.parse_search(read("rightmove_search.html")) if "Exmoor" in x.address)
    assert l.lat and l.lng and l.added_at and l.available
    rightmove.parse_detail(read("rightmove_detail.html"), l)
    assert l.council_tax_band == "B"
    assert l.parking_types == ["on_street"]
    assert l.garden_flag is True
    assert l.description.startswith("Ocean are delighted")


def test_onthemarket_detail_structured_fields():
    l = next(x for x in onthemarket.parse_search(read("otm_search.html")) if "Exmoor" in x.address)
    onthemarket.parse_detail(read("otm_detail.html"), l)
    assert (l.council_tax_band, l.epc, l.size_sqft) == ("B", "D", 753)
    assert l.lat and l.available


def test_openrent_detail_structured_fields():
    l = openrent.parse_search(read("openrent_search.html"))[0]
    assert l.lat and l.added_at
    openrent.parse_detail(read("openrent_detail.html"), l)
    assert (l.epc, l.parking_flag, l.available) == ("C", False, "2026-11-01")


@pytest.mark.parametrize("text,expected", [
    ("Allocated parking space", "dedicated"),
    ("Off-street parking for one car", "dedicated"),
    ("Single garage and driveway", "dedicated"),
    ("On street parking (permit zone)", "on-street"),
    ("RPZ on street", "on-street"),
    ("No parking", "unknown"),
    ("Lovely kitchen", "unknown"),
])
def test_classify_parking(text, expected):
    from rentbot.enrich import classify_parking
    assert classify_parking(Listing("x", "1", "", "", 1500, 2, description=text))[0] == expected


@pytest.mark.parametrize("text,expected", [
    ("Stunning new build apartment with lift", "modern"),
    ("Victorian terrace with original features", "period"),
    ("Two bed flat", "unknown"),
])
def test_classify_age(text, expected):
    from rentbot.enrich import classify_age
    assert classify_age(Listing("x", "1", "", "", 1500, 2, description=text))[0] == expected


def test_size_band_epc_and_council_tax():
    from rentbot.enrich import council_tax, extract_band, extract_epc, extract_size_sqft
    assert extract_size_sqft("approx 650 sq ft") == 650
    assert extract_size_sqft("Total area 60 sq m") == 646
    assert extract_band("Council Tax Band: C") == "C"
    assert extract_band("It falls under the Council Tax Band B") == "B"
    assert extract_epc("The property is rated D for energy performance") == "D"
    year, month = council_tax("B", {"band_d_annual": 2713.68, "discount": 0.25, "instalments": 12})
    assert (year, month) == (1582.98, 131.91)


def test_period_terrace_with_lift_word_is_not_modern():
    from rentbot.enrich import classify_age
    l = Listing("x", "1", "", "", 1500, 2, description="Stair lift not included. Terraced house close to the development site.")
    assert classify_age(l)[0] == "unknown"


def test_material_facts_lift_access_no_is_not_modern():
    from rentbot.enrich import classify_age
    l = Listing("x", "1", "", "", 1500, 2, description="Building Material type – Brick – Lift access – no")
    assert classify_age(l)[0] == "unknown"


def test_listings_without_photos_still_parse():
    letting = {"status": "To Let", "price": 1500}
    feed = {"data": [{"streetId": "a1", "publicAddress": "Exmoor Street, BS3", "bedrooms": 2,
                      "lettingsListing": letting, "media": []}]}
    assert bristolreslet.parse_search(feed)[0].image == ""
    otm = {"props": {"pageProps": {"initialReduxState": {"results": {"list": [
        {"id": 1, "details-url": "/details/1/", "price": "£1,500 pcm", "cover-image": "missing"}]}}}}}
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(otm)}</script>'
    assert onthemarket.parse_search(html)[0].image == ""
