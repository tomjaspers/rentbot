from dataclasses import asdict, dataclass, field


@dataclass
class Listing:
    source: str
    id: str
    url: str
    address: str
    price_pcm: int | None
    beds: int | None
    property_type: str = ""
    agent: str = ""
    image: str = ""
    summary: str = ""
    description: str = ""
    features: list[str] = field(default_factory=list)
    lat: float | None = None
    lng: float | None = None
    added_at: float | None = None    # when the site says it was listed (epoch seconds)
    added_label: str = ""            # the site's own wording, e.g. "Added yesterday", "Reduced today"
    available: str = ""              # "Now" or an ISO date (YYYY-MM-DD)
    size_sqft: int | None = None
    council_tax_band: str = ""
    epc: str = ""
    development: bool = False        # site flags it as a new-build development
    # Structured flags from the site, when it has them (None = unknown)
    garden_flag: bool | None = None
    parking_flag: bool | None = None
    parking_types: list[str] = field(default_factory=list)  # e.g. Rightmove's "on_street", "allocated"
    # Derived by rentbot.enrich
    garden: str = "no"               # yes | maybe | no
    garden_reason: str = ""
    parking: str = "unknown"         # dedicated | on-street | unknown
    parking_reason: str = ""
    age: str = "unknown"             # modern | period | unknown
    age_reason: str = ""
    walk_m: int | None = None        # walking route to the reference place (Aldi)
    walk_min: float | None = None
    council_tax_month: float | None = None  # after single person discount
    council_tax_year: float | None = None
    score: int = 0
    score_notes: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"

    def to_dict(self) -> dict:
        return asdict(self)
