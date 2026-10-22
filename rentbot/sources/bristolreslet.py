import json
import re

from rentbot.http import Http
from rentbot.models import Listing
from rentbot.sources.base import html_to_text, parse_available, parse_price_pcm

BASE = "https://bristolreslet.com"
# The site's own JSON feed (what its "Available Properties" grid pages through); ids are fixed per site.
COMPANY_ID = "019b8ede-123b-718c-a298-55ce03441cb4"
WEBSITE_ID = "01a15dc1-30d6-736a-90a0-43d87bdf86b7"
AGENT = "The Bristol Residential Letting Co"
# Their parking types, named like Rightmove's so enrich.classify_parking understands them.
PARKING_TYPES = {"onroad": "on_street", "offroad": "off_street"}


class BristolResLet:
    name = "bristolreslet"

    def __init__(self, cfg: dict, criteria: dict):
        self.criteria = criteria

    def search(self, http: Http) -> list[Listing]:
        params = {
            "website": WEBSITE_ID,
            "sort": "-lettings_listing_date",
            "listing_type": "to_let",
            "filters[min_bedrooms]": self.criteria["min_beds"],
            "filters[max_price_pcm]": self.criteria["max_price_pcm"],
            "per_page": 100,
        }
        out, page = [], 1
        while True:
            data = json.loads(http.get(f"{BASE}/api/property-feed/{COMPANY_ID}", params={**params, "page": page}))
            out += parse_search(data)
            if not data.get("next_page_url"):
                return out
            page += 1

    def enrich(self, http: Http, listing: Listing) -> Listing:
        parse_detail(http.get(listing.url), listing)
        return listing


def page_props(html: str) -> dict:
    m = re.search(r'<script data-page="app" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("page data not found (page layout changed or request blocked)")
    return json.loads(m.group(1))["props"]


def parse_search(data: dict) -> list[Listing]:
    out = []
    for p in data["data"]:
        letting = p.get("lettingsListing") or {}
        if letting.get("status") != "To Let":  # the feed includes "Let Agreed"
            continue
        address = p.get("publicAddress") or ""
        addr = p.get("address") or {}
        media = p.get("media") or []
        slug = re.sub(r"[^a-z0-9]+", "-", address.lower()).strip("-") or "property"
        out.append(
            Listing(
                source="bristolreslet",
                id=p["streetId"],
                url=f"{BASE}/properties-to-let/{slug}/{p['streetId']}",
                address=address,
                price_pcm=parse_price_pcm(f"£{letting.get('price') or ''}"),
                beds=p.get("bedrooms"),
                property_type=" ".join(filter(None, [p.get("style"), p.get("type")])),
                agent=AGENT,
                image=media[0].get("thumbnail") or media[0].get("url") or "" if media else "",
                summary=letting.get("shortDescription") or "",
                lat=addr.get("latitude"),
                lng=addr.get("longitude"),
                available=parse_available((letting.get("availableFrom") or "")[:10]),
            )
        )
    return out


def parse_detail(html: str, listing: Listing) -> None:
    prop = page_props(html)["property"]
    letting = prop.get("lettingsListing") or {}
    listing.features = [f["name"] for f in prop.get("features") or [] if f.get("name")]
    listing.description = html_to_text(letting.get("fullDescription") or "")
    listing.council_tax_band = (prop.get("councilTaxBand") or "").upper()[:1]
    listing.epc = ((prop.get("epc") or {}).get("energyEfficiencyRating") or "").upper()[:1]
    if prop.get("floorArea"):
        listing.size_sqft = round(prop["floorArea"] * 10.7639)
    if letting.get("availableFrom"):
        listing.available = parse_available(letting["availableFrom"][:10])
    if letting.get("newHome"):
        listing.development = True
    spaces = {(s.get("type") or "").lower() for s in prop.get("outsideSpaces") or []}
    if "garden" in spaces:
        listing.garden_flag = True
    listing.parking_types = [PARKING_TYPES.get(t, t) for t in
                             ((p.get("type") or "").lower() for p in prop.get("parking") or []) if t]
    addr = prop.get("address") or {}
    listing.lat = addr.get("latitude") or listing.lat
    listing.lng = addr.get("longitude") or listing.lng
