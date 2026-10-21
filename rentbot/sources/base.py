import json
import re
from datetime import date, datetime, timedelta
from typing import Protocol

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from rentbot.http import Http
from rentbot.models import Listing


class Source(Protocol):
    name: str

    def search(self, http: Http) -> list[Listing]: ...

    def enrich(self, http: Http, listing: Listing) -> Listing: ...


def next_data(html: str) -> dict:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        raise ValueError("__NEXT_DATA__ not found (page layout changed or request blocked)")
    return json.loads(m.group(1))


def html_to_text(fragment: str) -> str:
    if not fragment:
        return ""
    return HTMLParser(fragment).text(separator=" ").strip()


MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_date(text: str) -> date | None:
    """'06/11/2026', '2026-11-06T00:00:00Z', '6 Nov 2026', 'November 6, 2026' -> date."""
    t = (text or "").strip()
    try:
        if m := re.search(r"(\d{4})-(\d{2})-(\d{2})", t):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", t):
            return date(int(m[3]), int(m[2]), int(m[1]))
        if m := re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3})[a-z]*\.?,?\s+(\d{4})", t):
            return date(int(m[3]), MONTHS[m[2].lower()], int(m[1]))
        if m := re.search(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", t):
            return date(int(m[3]), MONTHS[m[1].lower()], int(m[2]))
    except (KeyError, ValueError):
        return None
    return None


def parse_available(text: str) -> str:
    """Normalise availability to 'Now' or an ISO date; '' when unknown."""
    t = (text or "").strip().lower()
    if not t:
        return ""
    if any(w in t for w in ("now", "immediate")):
        return "Now"
    d = parse_date(text)
    if d is None:
        return ""
    return "Now" if d <= date.today() else d.isoformat()


def parse_added(label: str, fallback_iso: str = "") -> float | None:
    """'Added today' / 'Reduced yesterday' / 'Added on 13/10/2026' -> epoch seconds."""
    t = (label or "").lower()
    today = datetime.combine(date.today(), datetime.min.time())
    if "today" in t:
        return today.timestamp()
    if "yesterday" in t:
        return (today - timedelta(days=1)).timestamp()
    if d := parse_date(label):
        return datetime.combine(d, datetime.min.time()).timestamp()
    if fallback_iso:
        try:
            return datetime.fromisoformat(fallback_iso.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return None


def parse_price_pcm(text: str) -> int | None:
    """'£1,550 pcm (£358 pw)' -> 1550; '£350 pw' -> 1517."""
    m = re.search(r"£\s?([\d,]+(?:\.\d+)?)\s*(pcm|pw|per month|per week|p/m|p/w)?", text or "", re.I)
    if not m:
        return None
    amount = float(m.group(1).replace(",", ""))
    if (m.group(2) or "").lower() in ("pw", "per week", "p/w"):
        amount = amount * 52 / 12
    return round(amount)
