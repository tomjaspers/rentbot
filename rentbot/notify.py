import html
import os
import time

import httpx

from rentbot.models import Listing

SOURCE_NAMES = {"rightmove": "Rightmove", "onthemarket": "OnTheMarket", "openrent": "OpenRent"}


class Telegram:
    """Listing alerts go to TELEGRAM_CHAT_ID (the group); failures go to TELEGRAM_ADMIN_CHAT_ID."""

    def __init__(self, token: str | None = None, chat_id: str | None = None, admin_chat_id: str | None = None,
                 dashboard_url: str | None = None):
        self.dashboard_url = dashboard_url
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        self.admin_chat_id = admin_chat_id or os.environ.get("TELEGRAM_ADMIN_CHAT_ID") or self.chat_id
        if not self.token or not self.chat_id:
            raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set (see .env.example)")

    def _call(self, method: str, **payload) -> dict:
        r = httpx.post(f"https://api.telegram.org/bot{self.token}/{method}", json=payload, timeout=20)
        if r.status_code >= 400:
            raise TelegramError(f"{method} failed: {r.status_code} {r.text[:300]}")
        return r.json()["result"]

    def _message(self, chat_id: str, text: str, reply_to: int | None = None) -> int:
        extra = {"reply_parameters": {"message_id": reply_to, "allow_sending_without_reply": True}} if reply_to else {}
        return self._call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                          link_preview_options={"is_disabled": True}, **extra)["message_id"]

    def send(self, text: str, reply_to: int | None = None) -> int:
        try:
            return self._message(self.chat_id, text, reply_to)
        except (TelegramError, httpx.HTTPError) as e:
            self.alert_admin(f"Couldn't post to the alerts chat: {e}")
            raise

    def send_listing(self, listing: Listing, headline: str = "New match") -> int:
        text = format_listing(listing, headline, dashboard_url=self.dashboard_url)
        if listing.image:
            try:
                return self._call("sendPhoto", chat_id=self.chat_id, photo=listing.image,
                                  caption=text, parse_mode="HTML")["message_id"]
            except (TelegramError, httpx.HTTPError):
                pass  # often just an image Telegram can't fetch; the text send below reports real failures
        return self.send(text)

    def update_listing(self, message_id: int, listing: Listing, also: list[tuple[str, str]]) -> None:
        """Rewrites an alert in place (photo alerts have a caption, text alerts have text)."""
        text = format_listing(listing, "New match", also, dashboard_url=self.dashboard_url)
        try:
            self._call("editMessageCaption", chat_id=self.chat_id, message_id=message_id,
                       caption=text, parse_mode="HTML")
        except TelegramError:
            self._call("editMessageText", chat_id=self.chat_id, message_id=message_id, text=text,
                       parse_mode="HTML", link_preview_options={"is_disabled": True})

    def alert_admin(self, text: str, level: str = "warn") -> None:
        """Everything that isn't a house alert goes to the admin's private chat. Never raises:
        reporting must not break the run."""
        icon = {"warn": "⚠️", "info": "ℹ️"}.get(level, "")
        try:
            self._message(self.admin_chat_id, f"{icon} rentbot: {html.escape(text)[:3500]}")
        except Exception as e:
            print(f"could not alert admin: {e!r}", flush=True)


class TelegramError(Exception):
    pass


def fmt_size(sqft: int | None) -> str:
    return f"{sqft:,} sq ft / {round(sqft / 10.7639)} m²" if sqft else ""


def fmt_walk(l: Listing) -> str:
    if l.walk_m is None:
        return ""
    return f"Aldi: {l.walk_min:g} min walk ({l.walk_m:,} m / {round(l.walk_m * 3.281):,} ft)"


def fmt_added(l: Listing) -> str:
    if l.added_at:
        days = (time.time() - l.added_at) / 86400
        when = "today" if days < 1 else "yesterday" if days < 2 else f"{days:.0f} days ago"
        s = f"Listed {when}"
    else:
        s = l.added_label
    if l.added_label.lower().startswith("reduced"):
        s += " (reduced)"
    return s


def format_listing(l: Listing, headline: str, also: list[tuple[str, str]] | None = None,
                   dashboard_url: str | None = None) -> str:
    e = html.escape
    garden = f"Garden: {l.garden}" + (f" ({e(l.garden_reason)})" if l.garden_reason else "")
    parking = {"dedicated": "Parking: dedicated ✅", "on-street": "Parking: on-street only"}.get(l.parking, "Parking: unknown")
    if l.parking == "dedicated" and l.parking_reason:
        parking += f" ({e(l.parking_reason)})"
    ct = (f"Council tax: band {l.council_tax_band} ≈ £{l.council_tax_month:,.0f}/mo after 25%"
          if l.council_tax_month else "Council tax: band unknown")
    building = {"modern": "Modern building", "period": "Period building"}.get(l.age, "")
    if building and l.age_reason:
        building += f" ({e(l.age_reason)})"
    facts = " · ".join(x for x in [fmt_size(l.size_sqft), f"EPC {l.epc}" if l.epc else "", building] if x)
    avail = f"Available: {'now' if l.available == 'Now' else l.available}" if l.available else ""
    lines = [
        f"<b>{e(headline)}: £{l.price_pcm:,} pcm · {l.beds} bed {e(l.property_type)}</b> · score {l.score}",
        e(l.address),
        garden,
        parking,
        e(fmt_walk(l)),
        facts,
        ct,
        " · ".join(x for x in [e(fmt_added(l)), avail] if x),
        f"{e(l.agent)} · {SOURCE_NAMES.get(l.source, l.source)}",
        f'<a href="{e(l.url)}">Open listing</a>',
    ]
    if also:
        lines.append("Also on: " + " · ".join(
            f'<a href="{e(url)}">{SOURCE_NAMES.get(src, src)}</a>' for src, url in also))
    if dashboard_url:
        lines.append(f'<a href="{e(dashboard_url)}">Dashboard</a> (look into / reject)')
    return "\n".join(line for line in lines if line.strip())
