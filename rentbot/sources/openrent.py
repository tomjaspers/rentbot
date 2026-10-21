import re

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from rentbot.http import Http
from rentbot.models import Listing
from rentbot.sources.base import parse_available

BASE = "https://www.openrent.co.uk"


class OpenRent:
    name = "openrent"

    def __init__(self, cfg: dict, criteria: dict):
        self.location = cfg["location"]
        self.criteria = criteria

    def search(self, http: Http) -> list[Listing]:
        # The page embeds every result (not just the rendered cards) as parallel JS arrays.
        params = {
            "prices_max": self.criteria["max_price_pcm"],
            "bedrooms_min": self.criteria["min_beds"],
            "bedrooms_max": self.criteria["max_beds"],
            "isLive": "true",
        }
        return parse_search(http.get(f"{BASE}/properties-to-rent/{self.location}", params=params))

    def enrich(self, http: Http, listing: Listing) -> Listing:
        parse_detail(http.get(listing.url), listing)
        return listing


def _js_array(html: str, name: str) -> list[str]:
    m = re.search(rf"var\s+{name}\s*=\s*\[(.*?)\]", html, re.S)
    if not m:
        return []
    return [v.strip().strip("'\"") for v in m.group(1).split(",") if v.strip()]


def parse_search(html: str) -> list[Listing]:
    ids = _js_array(html, "PROPERTYIDS")
    if not ids:
        raise ValueError("PROPERTYIDS not found (page layout changed or request blocked)")
    prices = _js_array(html, "prices")
    beds = _js_array(html, "bedrooms")
    gardens = _js_array(html, "gardens")
    live = _js_array(html, "islivelistBool")
    parking = _js_array(html, "parkings")
    lats = _js_array(html, "PROPERTYLISTLATITUDES")
    lngs = _js_array(html, "PROPERTYLISTLONGITUDES")
    listed_ms = _js_array(html, "dateFirstListedMs")

    # Rendered cards (first page only) give us the slug URL, title and photo.
    cards = {}
    for a in HTMLParser(html).css("a.pli"):
        m = re.search(r"/(\d+)$", a.attributes.get("href") or "")
        if m:
            img = a.css_first("img.propertyPic")
            cards[m.group(1)] = (
                BASE + a.attributes["href"],
                img.attributes.get("alt", "") if img else "",
                "https:" + img.attributes["src"] if img and img.attributes.get("src", "").startswith("//") else "",
            )

    def at(arr, i):
        return arr[i] if i < len(arr) else None

    out = []
    for i, pid in enumerate(ids):
        if at(live, i) == "0":
            continue
        url, title, image = cards.get(pid, (f"{BASE}/{pid}", "", ""))
        out.append(
            Listing(
                source="openrent",
                id=pid,
                url=url,
                address=title,
                price_pcm=int(float(at(prices, i))) if at(prices, i) else None,
                beds=int(at(beds, i)) if at(beds, i) else None,
                agent="OpenRent (private landlord)",
                image=image,
                garden_flag=(at(gardens, i) == "1") if at(gardens, i) is not None else None,
                parking_flag=(at(parking, i) == "1") if at(parking, i) is not None else None,
                lat=float(at(lats, i)) if at(lats, i) else None,
                lng=float(at(lngs, i)) if at(lngs, i) else None,
                added_at=int(at(listed_ms, i)) / 1000 if at(listed_ms, i) else None,
            )
        )
    return out


def parse_detail(html: str, listing: Listing) -> None:
    tree = HTMLParser(html)
    h1 = tree.css_first("h1")
    if h1:
        listing.address = h1.text(strip=True)
        lowered = listing.address.lower()
        for kind in ("flat", "house", "maisonette", "studio", "bungalow"):
            if kind in lowered:
                listing.property_type = kind
                break
    desc = tree.css_first(".description-text") or tree.css_first("#descriptionText")
    if desc:
        listing.description = desc.text(separator=" ").strip()
    og = tree.css_first('meta[property="og:url"]')
    if og and og.attributes.get("content"):
        listing.url = og.attributes["content"].replace("http://", "https://")
    for row in tree.css("tr"):
        cells = row.css("td")
        if len(cells) != 2:
            continue
        label, value = cells[0].text(strip=True), cells[1].text(strip=True)
        # Yes/no rows render a tick (text-success) or cross (text-danger) icon.
        if label == "Garden":
            listing.garden_flag = "text-success" in cells[1].html
        elif label == "Parking":
            listing.parking_flag = "text-success" in cells[1].html
        elif label == "EPC Rating":
            listing.epc = value[:1].upper()
        elif label == "Available From":
            listing.available = parse_available(value)
