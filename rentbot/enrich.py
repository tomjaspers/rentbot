"""Derived facts that need the full listing text: parking, building age, size, council tax,
walking distance and the overall score."""

import math
import re
import time

import httpx

from rentbot.models import Listing

# --- text extraction ---

SQFT = re.compile(r"(\d[\d,]{1,5}(?:\.\d+)?)\s*(?:sq\.?\s*f(?:ee)?t\.?|sqft|square\s+f(?:ee|oo)t|ft²|ft2)", re.I)
SQM = re.compile(r"(\d[\d,]{0,4}(?:\.\d+)?)\s*(?:sq\.?\s*m(?:etres|eters)?\.?|sqm|square\s+met(?:re|er)s?|m²|m2)\b", re.I)
COUNCIL_BAND = re.compile(r"council\s*tax(?:\s*band)?\s*[:\-–]?\s*(?:band\s*)?([A-H])\b", re.I)
EPC = re.compile(r"(?:\bEPC(?:\s*rating)?|energy\s+rating|rated)\s*[:\-–]?\s*(?:band\s*)?([A-G])\b(?:\s*for\s*energy)?", re.I)


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def extract_size_sqft(text: str) -> int | None:
    sizes = [_num(m.group(1)) for m in SQFT.finditer(text)]
    sizes = [s for s in sizes if 150 <= s <= 5000]
    if sizes:
        return round(max(sizes))
    sizes = [_num(m.group(1)) for m in SQM.finditer(text)]
    sizes = [s for s in sizes if 15 <= s <= 500]
    return round(max(sizes) * 10.7639) if sizes else None


def extract_band(text: str) -> str:
    m = COUNCIL_BAND.search(text)
    return m.group(1).upper() if m else ""


def extract_epc(text: str) -> str:
    m = EPC.search(text)
    return m.group(1).upper() if m else ""


# --- parking ---

DEDICATED = re.compile(
    r"\b(allocated|private|secure|gated|underground|dedicated|designated|own|off[- ]?(?:street|road))\s+"
    r"(?:car\s+)?(?:parking|parking\s+space|space|bay)s?\b|\bdriveway\b|\bgarage\b|\bcar\s*port\b|"
    r"\bparking\s+(?:space|bay)\b|\boff[- ]?(?:street|road)\s+parking\b",
    re.I,
)
ON_STREET = re.compile(r"\bon[- ]?street\b|\bpermit\b|\bresidents'?\s+parking\b|\bRPZ\b|\bparking\s+zone\b", re.I)
NO_PARKING = re.compile(r"\bno\s+(?:off[- ]street\s+|allocated\s+|private\s+)?parking\b", re.I)
RM_DEDICATED = {"allocated", "off_street", "driveway", "garage", "private", "gated", "covered", "underground", "secure"}


def classify_parking(l: Listing) -> tuple[str, str]:
    types = {t.lower() for t in l.parking_types}
    if types & RM_DEDICATED:
        return "dedicated", ", ".join(sorted(types & RM_DEDICATED)).replace("_", " ")
    text = NO_PARKING.sub(" ", " ".join(l.features + [l.summary, l.description]))
    if m := DEDICATED.search(text):
        # "on street parking space" is still on-street
        if "street" not in text[max(0, m.start() - 12):m.start()].lower():
            return "dedicated", m.group(0)
    on_street = ON_STREET.search(text)
    if l.parking_flag and not types and not on_street:
        return "dedicated", "parking listed in the site's details"
    if types or on_street:
        return "on-street", ", ".join(sorted(types)).replace("_", " ") or "on street"
    return "unknown", ""


# --- building age ---

MODERN = re.compile(
    r"\bnew[- ]?build\b|\bnewly[- ]built\b|\brecently\s+built\b|\bbuilt\s+in\s+20\d\d\b|\bcompleted\s+in\s+20\d\d\b|"
    r"\bpurpose[- ]built\b|\bcontemporary\b|\bmodern\s+(?:apartment|flat|home|house|development|building|block|townhouse)\b|"
    r"\b(?:new|modern|recent)\s+development\b|\bconcierge\b",
    re.I,
)
PERIOD = re.compile(
    r"\b(victorian|edwardian|georgian|regency|period|1930s|character)\b|\boriginal\s+features\b|\bhigh\s+ceilings\b",
    re.I,
)


def classify_age(l: Listing) -> tuple[str, str]:
    text = " ".join(l.features + [l.summary, l.description])
    if l.development:
        return "modern", "new-build development"
    period = PERIOD.search(text)
    modern = MODERN.search(text)
    if modern and not period:
        return "modern", modern.group(0)
    if period and not modern:
        return "period", period.group(0)
    if l.epc in ("A", "B"):
        return "modern", f"EPC {l.epc}"
    if period:
        return "period", period.group(0)
    return "unknown", ""


# --- council tax ---

BAND_RATIO = {"A": 6, "B": 7, "C": 8, "D": 9, "E": 11, "F": 13, "G": 15, "H": 18}  # ninths of band D


def council_tax(band: str, cfg: dict) -> tuple[float, float] | None:
    if band not in BAND_RATIO:
        return None
    year = cfg["band_d_annual"] * BAND_RATIO[band] / 9 * (1 - cfg.get("discount", 0))
    return round(year, 2), round(year / cfg.get("instalments", 12), 2)


# --- walking distance ---

def haversine_m(lat1, lng1, lat2, lng2) -> float:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def walking(lat: float, lng: float, dest: dict) -> tuple[int, float, bool]:
    """(metres, minutes, routed). Uses the public FOSSGIS Valhalla router (OpenStreetMap); if that
    fails, estimates from straight-line distance."""
    try:
        r = httpx.post(
            "https://valhalla1.openstreetmap.de/route",
            json={"locations": [{"lat": lat, "lon": lng}, {"lat": dest["lat"], "lon": dest["lng"]}],
                  "costing": "pedestrian", "units": "kilometers"},
            headers={"User-Agent": "rentbot/0.1 (personal rental search)"},
            timeout=15,
        )
        r.raise_for_status()
        s = r.json()["trip"]["summary"]
        time.sleep(1)  # fair use of a free public service
        return round(s["length"] * 1000), round(s["time"] / 60, 1), True
    except (httpx.HTTPError, KeyError, ValueError):
        m = haversine_m(lat, lng, dest["lat"], dest["lng"]) * 1.3  # typical street-network detour
        return round(m), round(m / 80, 1), False  # ~4.8 km/h


# --- score ---

def score(l: Listing, now: float | None = None) -> tuple[int, list[str]]:
    """Higher is better. Only used for ranking; the hard criteria live in filters.py."""
    now = now or time.time()
    pts, notes = 0, []

    def add(n, why):
        nonlocal pts
        pts += n
        notes.append(f"{'+' if n >= 0 else ''}{n} {why}")

    add({"yes": 3, "maybe": 1}.get(l.garden, 0), f"garden {l.garden}")
    if l.parking == "dedicated":
        add(3, "dedicated parking")
    if l.walk_min is not None:
        add(3 if l.walk_min <= 5 else 2 if l.walk_min <= 10 else 1 if l.walk_min <= 15 else 0, f"{l.walk_min:g} min to Aldi")
    if l.added_at:
        days = (now - l.added_at) / 86400
        add(2 if days < 1 else 1 if days < 3 else 0 if days < 14 else -2, f"listed {days:.0f}d ago")
    if l.added_label.lower().startswith("reduced"):
        add(-1, "price reduced (been on the market a while)")
    add({"modern": 2, "period": -1}.get(l.age, 0), f"{l.age} building")
    if l.size_sqft:
        add(1 if l.size_sqft >= 700 else 0 if l.size_sqft >= 550 else -1, f"{l.size_sqft} sq ft")
    return pts, notes


def apply(l: Listing, cfg: dict) -> None:
    from rentbot.filters import classify_garden

    text = " ".join(l.features + [l.summary, l.description])
    l.size_sqft = l.size_sqft or extract_size_sqft(text)
    if l.council_tax_band not in BAND_RATIO:  # sites sometimes say "TBC"
        l.council_tax_band = extract_band(text)
    l.epc = l.epc or extract_epc(text)
    l.garden, l.garden_reason = classify_garden(l)
    l.parking, l.parking_reason = classify_parking(l)
    l.age, l.age_reason = classify_age(l)
    if ct := council_tax(l.council_tax_band, cfg["council_tax"]):
        l.council_tax_year, l.council_tax_month = ct
    place = cfg.get("walk_to")
    if place and l.lat and l.lng and l.walk_m is None:
        l.walk_m, l.walk_min, _ = walking(l.lat, l.lng, place)
    l.score, l.score_notes = score(l)
