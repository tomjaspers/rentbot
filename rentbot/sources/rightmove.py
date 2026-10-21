import json
import re

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from rentbot.http import Http
from rentbot.models import Listing
from rentbot.sources.base import html_to_text, next_data, parse_added, parse_available

BASE = "https://www.rightmove.co.uk"


class Rightmove:
    name = "rightmove"

    def __init__(self, cfg: dict, criteria: dict):
        self.location = cfg["location_identifier"]
        self.criteria = criteria

    def search_params(self) -> dict:
        return {
            "locationIdentifier": self.location,
            "minBedrooms": self.criteria["min_beds"],
            "maxBedrooms": self.criteria["max_beds"],
            "maxPrice": self.criteria["max_price_pcm"],
            "sortType": 6,  # newest listed first
            "index": 0,
            "includeLetAgreed": "false",
        }

    def search(self, http: Http) -> list[Listing]:
        html = http.get(f"{BASE}/property-to-rent/find.html", params=self.search_params())
        return parse_search(html)

    def enrich(self, http: Http, listing: Listing) -> Listing:
        parse_detail(http.get(listing.url), listing)
        return listing


def parse_search(html: str) -> list[Listing]:
    props = next_data(html)["props"]["pageProps"]["searchResults"]["properties"]
    out = []
    for p in props:
        price = p.get("price") or {}
        amount = price.get("amount")
        if amount and price.get("frequency") == "weekly":
            amount = round(amount * 52 / 12)
        images = p.get("images") or []
        loc = p.get("location") or {}
        size = re.search(r"([\d,]+)\s*sq", p.get("displaySize") or "")
        out.append(
            Listing(
                source="rightmove",
                id=str(p["id"]),
                url=f"{BASE}/properties/{p['id']}",
                address=p.get("displayAddress") or "",
                price_pcm=amount,
                beds=p.get("bedrooms"),
                property_type=p.get("propertySubType") or "",
                agent=(p.get("customer") or {}).get("branchDisplayName") or "",
                image=images[0]["srcUrl"] if images else "",
                summary=p.get("summary") or "",
                lat=loc.get("latitude"),
                lng=loc.get("longitude"),
                added_label=p.get("addedOrReduced") or "",
                added_at=parse_added(p.get("addedOrReduced") or "", p.get("firstVisibleDate") or ""),
                available=parse_available(p.get("letAvailableDate") or ""),
                size_sqft=int(size.group(1).replace(",", "")) if size else None,
                development=bool(p.get("development")),
            )
        )
    return out


def page_model(html: str) -> dict | None:
    """window.__PAGE_MODEL holds the listing as a flattened array where every value is an index
    into that array (devalue-style); rebuild the nested object."""
    i = html.find("window.__PAGE_MODEL = ")
    if i < 0:
        return None
    outer, _ = json.JSONDecoder().raw_decode(html, i + len("window.__PAGE_MODEL = "))
    data = outer.get("data")
    if not isinstance(data, str):
        return outer
    arr = json.loads(data)

    def hydrate(ix, depth=0):
        if not isinstance(ix, int) or ix < 0 or ix >= len(arr) or depth > 40:
            return None
        v = arr[ix]
        if isinstance(v, dict):
            return {k: hydrate(x, depth + 1) for k, x in v.items()}
        if isinstance(v, list):
            return [hydrate(x, depth + 1) for x in v]
        return v

    return hydrate(0)


def parse_detail(html: str, listing: Listing) -> None:
    try:
        pd = (page_model(html) or {}).get("propertyData") or {}
    except (ValueError, AttributeError):
        pd = {}
    if pd:
        listing.features = [f for f in pd.get("keyFeatures") or [] if isinstance(f, str)]
        listing.description = html_to_text((pd.get("text") or {}).get("description") or "")
        listing.council_tax_band = ((pd.get("livingCosts") or {}).get("councilTaxBand") or "").upper()
        feats = pd.get("features") or {}
        listing.parking_types = [p.get("alias") for p in feats.get("parking") or [] if p and p.get("alias")]
        gardens = [g for g in feats.get("garden") or [] if g and (g.get("displayText") or "").lower() != "no"]
        if any("communal" not in (g.get("alias") or "") for g in gardens):
            listing.garden_flag = True
        for s in pd.get("sizings") or []:
            if s and s.get("unit") == "sqft" and s.get("maximumSize"):
                listing.size_sqft = round(s["maximumSize"])
        avail = (pd.get("lettings") or {}).get("letAvailableDate")
        if avail:
            listing.available = parse_available(avail)
        loc = pd.get("location") or {}
        listing.lat = loc.get("latitude") or listing.lat
        listing.lng = loc.get("longitude") or listing.lng
    if not listing.description:  # fall back to the rendered HTML
        tree = HTMLParser(html)
        listing.features = listing.features or [li.text(strip=True) for li in tree.css('[data-testid="keyFeatures"] li')]
        for h2 in tree.css("h2"):
            if h2.text(strip=True) == "Description" and h2.next is not None:
                listing.description = h2.next.text(separator=" ").strip()
                break
