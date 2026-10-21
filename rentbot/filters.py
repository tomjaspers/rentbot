import re

from rentbot.models import Listing

POSTCODE_DISTRICT = re.compile(r"\b([A-Z]{1,2}\d{1,2}[A-Z]?)(?:\s*\d[A-Z]{2})?\b")

NEGATIVE = re.compile(r"\b(no|without|lack of)\s+(private\s+|outside\s+|outdoor\s+)?(garden|outside space|outdoor space)", re.I)
GARDEN_YES = re.compile(
    r"\b(private|rear|front|enclosed|south[- ]facing|west[- ]facing|walled|lawned|own|large|small|sunny)?\s*"
    r"(garden|courtyard|patio|roof terrace|terrace(?!d)|outside space|outdoor space|yard)\b",
    re.I,
)
COMMUNAL = re.compile(r"\b(communal|shared)\s+(garden|courtyard|grounds|roof terrace)", re.I)
BALCONY = re.compile(r"\b(balcony|balconies|juliet)\b", re.I)


def postcode_districts(text: str) -> set[str]:
    return set(POSTCODE_DISTRICT.findall((text or "").upper()))


def classify_garden(listing: Listing) -> tuple[str, str]:
    """Returns (yes|maybe|no, reason). Only the description and features are used, never the
    address, because street names like 'Sidmouth Gardens' would otherwise count."""
    text = " ".join(listing.features + [listing.summary, listing.description])
    text = NEGATIVE.sub(" ", text)
    if listing.garden_flag:
        return "yes", "garden listed in the site's details"
    communal = COMMUNAL.search(text)
    private = GARDEN_YES.search(COMMUNAL.sub(" ", text))
    if private:
        return "yes", private.group(0).strip()
    if communal:
        return "maybe", communal.group(0)
    if BALCONY.search(text):
        return "maybe", "balcony"
    return "no", ""


def basic_match(listing: Listing, c: dict) -> tuple[bool, str]:
    """Criteria checkable from search results alone (before fetching the detail page)."""
    if listing.price_pcm is None or listing.price_pcm > c["max_price_pcm"]:
        return False, f"price {listing.price_pcm}"
    if listing.beds is None or not (c["min_beds"] <= listing.beds <= c["max_beds"]):
        return False, f"beds {listing.beds}"
    districts = postcode_districts(listing.address)
    allowed = {d.upper() for d in c.get("postcode_districts") or []}
    if allowed and districts and not districts & allowed:
        return False, f"district {','.join(sorted(districts))}"
    types = c.get("property_types", "any")
    if types != "any" and listing.property_type:
        if not any(t.lower() in listing.property_type.lower() for t in types):
            return False, f"type {listing.property_type}"
    return True, ""


def full_match(listing: Listing, c: dict) -> tuple[bool, str]:
    ok, why = basic_match(listing, c)
    if not ok:
        return ok, why
    listing.garden, listing.garden_reason = classify_garden(listing)
    mode = c.get("garden", "required")
    if mode == "required":
        if listing.garden == "no":
            return False, "no garden"
        if listing.garden == "maybe" and listing.garden_reason == "balcony" and not c.get("alert_on_balcony", True):
            return False, "balcony only"
    return True, ""
