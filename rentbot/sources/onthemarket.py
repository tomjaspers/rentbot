import re

from rentbot.http import Http
from rentbot.models import Listing
from rentbot.sources.base import html_to_text, next_data, parse_added, parse_available, parse_price_pcm

BASE = "https://www.onthemarket.com"


class OnTheMarket:
    name = "onthemarket"

    def __init__(self, cfg: dict, criteria: dict):
        self.location = cfg["location"]
        self.criteria = criteria

    def search(self, http: Http) -> list[Listing]:
        params = {
            "min-bedrooms": self.criteria["min_beds"],
            "max-bedrooms": self.criteria["max_beds"],
            "max-price": self.criteria["max_price_pcm"],
            "sort-field": "update_date",
            "let-agreed": "false",
        }
        html = http.get(f"{BASE}/to-rent/property/{self.location}/", params=params)
        return parse_search(html)

    def enrich(self, http: Http, listing: Listing) -> Listing:
        parse_detail(http.get(listing.url), listing)
        return listing


def parse_search(html: str) -> list[Listing]:
    items = next_data(html)["props"]["pageProps"]["initialReduxState"]["results"]["list"]
    out, seen = [], set()
    for p in items:
        if p["id"] in seen:  # spotlight listings repeat inside the results
            continue
        seen.add(p["id"])
        cover = p.get("cover-image") or {}
        loc = p.get("location") or {}
        label = p.get("days-since-added-reduced") or ""
        out.append(
            Listing(
                source="onthemarket",
                id=str(p["id"]),
                url=BASE + p["details-url"],
                address=p.get("address") or "",
                price_pcm=parse_price_pcm(p.get("price") or ""),
                beds=p.get("bedrooms"),
                property_type=p.get("humanised-property-type") or "",
                agent=(p.get("agent") or {}).get("name") or "",
                image=(cover.get("default") or cover.get("webp") or "") if isinstance(cover, dict) else "",
                features=list(p.get("features") or []),
                lat=loc.get("lat"),
                lng=loc.get("lon"),
                added_label=label,
                added_at=parse_added(label),  # only exact for today/yesterday; "< 7 days" stays unknown
            )
        )
    return out


def parse_detail(html: str, listing: Listing) -> None:
    prop = next_data(html)["props"]["pageProps"]["initialReduxState"]["property"]
    listing.features = [f["feature"] for f in prop.get("features") or [] if f.get("feature")]
    listing.description = html_to_text(prop.get("description") or "")
    for info in prop.get("keyInfo") or []:
        if (info.get("title") or "").lower() == "council tax":
            m = re.search(r"band\s*([A-H])\b", info.get("value") or "", re.I)
            listing.council_tax_band = m.group(1).upper() if m else ""
    listing.epc = ((prop.get("epc") or {}).get("rating") or "").upper()[:1]
    if prop.get("minimumAreaSqM"):
        listing.size_sqft = round(prop["minimumAreaSqM"] * 10.7639)
    for item in (prop.get("lettingDetails") or {}).get("items") or []:
        if item.lower().startswith("availab"):
            listing.available = parse_available(item.split(":", 1)[-1])
    if prop.get("developmentProperty") or prop.get("newHomeFlag"):
        listing.development = True
    loc = prop.get("location") or {}
    listing.lat = loc.get("lat") or listing.lat
    listing.lng = loc.get("lon") or listing.lng
