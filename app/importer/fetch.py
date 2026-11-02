"""Fetching index pages and documents, extracting and matching links."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from ..profiles import DocumentSpec, Source

PROJECT_URL = "https://github.com/andygruber/lunchtime"
USER_AGENT = f"Mozilla/5.0 (compatible; LunchtimeBot/1.0; +{PROJECT_URL})"


BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "de-AT,de;q=0.9,en;q=0.8",
}


def user_agent(contact_url: str | None = None) -> str:
    """Honest bot identification with a contact URL, so site operators can see who fetches and allow it."""
    if contact_url and "example." not in contact_url:
        return f"Mozilla/5.0 (compatible; LunchtimeBot/1.0; +{contact_url})"
    return USER_AGENT


def request_headers(contact_url: str | None = None, user_agent_mode: str | None = None) -> dict[str, str]:
    """Headers for a source: the honest bot identity by default; `browser` sends ordinary browser headers
    (opt-in per source, for hosts that reject every non-browser client); any other value is a custom
    User-Agent string."""
    if user_agent_mode == "browser":
        return dict(BROWSER_HEADERS)
    if user_agent_mode:
        return {"User-Agent": user_agent_mode}
    return {"User-Agent": user_agent(contact_url)}


TIMEOUT = httpx.Timeout(30.0, connect=10.0)
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024


@dataclass(frozen=True)
class Link:
    url: str
    label: str

    @property
    def is_pdf(self) -> bool:
        return urlparse(self.url).path.lower().endswith(".pdf")


def client(contact_url: str | None = None, user_agent_mode: str | None = None) -> httpx.Client:
    return httpx.Client(headers=request_headers(contact_url, user_agent_mode), timeout=TIMEOUT,
                        follow_redirects=True)


def fetch_text(http: httpx.Client, url: str) -> str:
    r = http.get(url)
    r.raise_for_status()
    return r.text


def fetch_bytes(http: httpx.Client, url: str) -> bytes:
    return fetch_document(http, url)[0]


def fetch_document(http: httpx.Client, url: str) -> tuple[bytes, str]:
    """(content, media type). The media type is sniffed from the content when the server is vague."""
    with http.stream("GET", url) as r:
        r.raise_for_status()
        chunks, size = [], 0
        for chunk in r.iter_bytes():
            size += len(chunk)
            if size > MAX_DOCUMENT_BYTES:
                raise ValueError(f"document larger than {MAX_DOCUMENT_BYTES} bytes")
            chunks.append(chunk)
        data = b"".join(chunks)
        header = r.headers.get("content-type", "").split(";")[0].strip().lower()
    return data, sniff_media_type(data, header)


def sniff_media_type(data: bytes, declared: str = "") -> str:
    head = data[:16]
    if head.startswith(b"%PDF"):
        return "application/pdf"
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if declared in ("text/html", "application/xhtml+xml") or data.lstrip()[:1] == b"<":
        return "text/html"
    return declared or "application/octet-stream"


EXTENSIONS = {"application/pdf": "pdf", "text/html": "html", "image/png": "png", "image/jpeg": "jpg",
              "image/gif": "gif", "image/webp": "webp"}


def page_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return clean_label(soup.title.get_text(" ", strip=True)) if soup.title else ""


def _region(html: str, selector: str | None):
    soup = BeautifulSoup(html, "html.parser")
    if not selector:
        return [soup]
    return soup.select(selector)


def extract_images(html: str, base_url: str, selector: str | None = None) -> list[Link]:
    """Content images (largest srcset candidate) with alt/title text as label, in page order."""
    out: list[Link] = []
    seen: set[str] = set()
    for root in _region(html, selector):
        for img in root.find_all("img"):
            src = img.get("data-src") or img.get("src") or ""
            srcset = img.get("data-srcset") or img.get("srcset") or ""
            if srcset:
                candidates = []
                for part in srcset.split(","):
                    bits = part.strip().split()
                    if bits:
                        width = int(bits[1][:-1]) if len(bits) > 1 and bits[1].endswith("w") and bits[1][:-1].isdigit() else 0
                        candidates.append((width, bits[0]))
                if candidates:
                    src = max(candidates)[1]
            if not src or src.startswith("data:") or src.lower().endswith(".svg"):
                continue
            url = urljoin(base_url, src)
            if url in seen:
                continue
            seen.add(url)
            out.append(Link(url, clean_label(img.get("alt") or img.get("title") or "")))
    return out


def clean_label(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s*\[\s*[\d.,]+\s*[KMG]?B\s*\]$", "", text, flags=re.I)  # "Speiseplan [113 KB]"


def extract_links(html: str, base_url: str, selector: str | None = None) -> list[Link]:
    """All <a href> links with absolute URL and visible text (or title/aria-label), deduplicated."""
    seen: dict[tuple[str, str], Link] = {}
    anchors = [a for root in _region(html, selector) for a in root.find_all("a", href=True)]
    for a in anchors:
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base_url, href)
        label = clean_label(a.get_text(" ", strip=True) or a.get("title") or a.get("aria-label") or "")
        if not label:
            label = urlparse(url).path.rsplit("/", 1)[-1]
        seen.setdefault((url, label), Link(url, label))
    return list(seen.values())


def match_document(links: list[Link], doc: DocumentSpec) -> list[Link]:
    out, urls = [], set()
    for link in links:
        if doc.match.matches(link.label, link.url) and link.url not in urls:
            urls.add(link.url)
            out.append(link)
    return out


def match_source(links: list[Link], source: Source) -> dict[tuple[str, str], list[Link]]:
    """(location id, document id) -> matching links."""
    return {
        (loc.id, doc.id): match_document(links, doc)
        for loc in source.locations
        for doc in loc.documents
    }


def suggest_label_pattern(label: str) -> str:
    """A starting regex for a link label: the leading words before dates/week numbers/sizes."""
    words = []
    for w in label.split():
        if re.search(r"\d", w) or re.fullmatch(r"(?i)kw\.?", w):
            break
        words.append(re.escape(w))
    return "^" + r"\s+".join(words) if words else re.escape(label)
