import re

from rentbot.models import Listing

STREET_WORDS = r"(road|street|avenue|lane|drive|close|gardens|terrace|place|way|crescent|hill|parade|walk|court|mews|rise|grove|view|square|park|row|vale)"


def street(address: str) -> str:
    """Best-effort street name: 'Exmoor Street - Southville' and 'Exmoor Street, Bristol BS3'
    both give 'exmoor street'."""
    parts = [p.strip() for p in re.split(r",| - | – ", address or "") if p.strip()]
    for p in parts:
        if re.search(rf"\b{STREET_WORDS}\b", p, re.I):
            return re.sub(r"[^a-z ]", "", re.sub(r"^\d+[a-z]?\s+", "", p.lower())).strip()
    return re.sub(r"[^a-z ]", "", parts[0].lower()).strip() if parts else ""


def fingerprint(listing: Listing) -> str:
    return f"{street(listing.address)}|{listing.beds}|{listing.price_pcm}"
