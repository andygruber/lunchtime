"""Fill missing coordinates of locations via OpenStreetMap Nominatim (max. 1 request per second)."""

from __future__ import annotations

import re
import time

import httpx

from .importer.fetch import USER_AGENT
from .profiles import ProfileStore

NOMINATIM = "https://nominatim.openstreetmap.org/search"


def lookup(http: httpx.Client, query: str) -> tuple[float, float, str] | None:
    r = http.get(NOMINATIM, params={"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "at"})
    r.raise_for_status()
    hits = r.json()
    if not hits:
        return None
    return float(hits[0]["lat"]), float(hits[0]["lon"]), hits[0].get("display_name", "")


def fill_missing(store: ProfileStore, force: bool = False, delay: float = 1.1) -> list[str]:
    """Geocode locations without `geo` (all with force). Falls back to postcode + town, then the town."""
    report = []
    with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=20) as http:
        for ref in store.locations():
            loc = ref.location
            if loc.geo and not force:
                continue
            queries = []
            if loc.address:
                queries.append((loc.address, None))
                split = re.sub(r"\b(\w+er)(straße|gasse)\b", lambda m: f"{m.group(1)} {m.group(2).capitalize()}",
                               loc.address)  # "Wienerstraße" → "Wiener Straße" as in OSM
                if split != loc.address:
                    queries.append((split, None))
                if "," in loc.address:
                    queries.append((loc.address.split(",", 1)[1].strip(), "approximate: postcode/town"))
            if loc.city:
                queries.append((loc.city, "approximate: town centre"))
            for query, precision in queries:
                time.sleep(delay)
                hit = lookup(http, query)
                postcode = re.search(r"\b\d{4}\b", query)
                if hit and postcode and postcode.group(0) not in hit[2]:
                    hit = None  # same street name in another town
                if hit:
                    lat, lon, name = hit
                    store.update_location(ref.source.id, loc.id, {"geo": {"lat": round(lat, 6), "lon": round(lon, 6)}},
                                          comment=precision)
                    report.append(f"{ref.key}: {lat:.5f}, {lon:.5f} ({precision or 'address'}) – {name}")
                    break
            else:
                report.append(f"{ref.key}: not found")
    return report
