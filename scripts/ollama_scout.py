# /// script
# requires-python = ">=3.11,<3.12"
# dependencies = [
#     "trafilatura",
#     "httpx",
#     "beautifulsoup4",
#     "ollama",
# ]
# ///
"""
Feature 3 — Active Web Scout
Reads 0-raw/reading-list.md, fetches all [ ] URLs, extracts clean article
text using trafilatura (the best open-source article extractor), and saves
full text to sources/. Creates .intake pointer files for the Refinery.

Also supports monitoring RSS feeds defined in reading-list.md.

Usage:
    uv run scripts/ollama_scout.py
"""

import os
import re
import sys
import datetime
import hashlib
import ipaddress
import json
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
import httpx
import httpcore

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (
    BRIEFINGS_DIR,
    READING_LIST,
    SOURCES_DIR,
    RAW_DIR,
    SIDECAR_DIR,
    _is_reparse_or_link,
    today,
    ensure_dirs,
)
from review_atom import atomic_write_bytes, review_lock

try:
    import trafilatura
    from trafilatura.settings import use_config
    HAS_TRAFILATURA = True
except ImportError:
    HAS_TRAFILATURA = False

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False


# ─── URL Fetching ─────────────────────────────────────────────────────────────

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
ALLOWED_WEB_PORTS = {80, 443}


class UnsafeRemoteUrlError(ValueError):
    """The Scout must not connect to this URL from the local machine."""


@dataclass(frozen=True)
class ValidatedRemoteTarget:
    url: str
    hostname: str
    port: int
    addresses: tuple[str, ...]


def _normalized_hostname(hostname: str) -> str:
    return hostname.rstrip(".").encode("idna").decode("ascii").lower()


_NAT64_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),      # RFC 6052 Well-Known NAT64 Prefix
    ipaddress.ip_network("64:ff9b:1::/48"),    # RFC 8215 Local-Use NAT64 Prefix
)


def is_public_ip_address(raw_address: str) -> bool:
    """Return True only for globally routable public IPs, rejecting NAT64 and IPv4-mapped internal addresses."""
    address = ipaddress.ip_address(raw_address)
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped.is_global
        if any(address in net for net in _NAT64_NETWORKS):
            return False
    return address.is_global


def validate_remote_url(url: str) -> ValidatedRemoteTarget:
    """Resolve once, reject non-public answers, and retain those exact addresses for connect."""
    try:
        parsed = urlsplit(str(url).strip())
    except ValueError as exc:
        raise UnsafeRemoteUrlError(f"invalid URL: {exc}") from exc
    if parsed.scheme not in {"http", "https"}:
        raise UnsafeRemoteUrlError("only http and https URLs are allowed")
    if not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeRemoteUrlError("URL must include a hostname and no credentials")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeRemoteUrlError(f"invalid URL port: {exc}") from exc
    if port is not None and port not in ALLOWED_WEB_PORTS:
        raise UnsafeRemoteUrlError("only standard HTTP(S) ports are allowed")

    port = port or (443 if parsed.scheme == "https" else 80)
    try:
        answers = socket.getaddrinfo(
            parsed.hostname,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise UnsafeRemoteUrlError(f"hostname could not be resolved: {exc}") from exc
    if not answers:
        raise UnsafeRemoteUrlError("hostname did not resolve to an address")
    addresses = []
    for answer in answers:
        raw_ip = answer[4][0]
        address = ipaddress.ip_address(raw_ip)
        if not is_public_ip_address(str(address)):
            raise UnsafeRemoteUrlError(f"non-public target address is not allowed: {address}")
        if str(address) not in addresses:
            addresses.append(str(address))
    if not addresses:
        raise UnsafeRemoteUrlError("hostname did not resolve to an address")
    return ValidatedRemoteTarget(
        url=parsed.geturl(),
        hostname=_normalized_hostname(parsed.hostname),
        port=port,
        addresses=tuple(addresses),
    )


class PinnedNetworkBackend(httpcore.NetworkBackend):
    """Connect only to the IPs captured by validation while keeping the URL host for TLS/Host."""

    def __init__(self, target: ValidatedRemoteTarget, backend=None):
        self.target = target
        self.backend = backend or httpcore.SyncBackend()

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if isinstance(host, bytes):
            host = host.decode("ascii")
        if _normalized_hostname(str(host)) != self.target.hostname or port != self.target.port:
            raise UnsafeRemoteUrlError("request attempted a connection outside its validated host and port")
        errors = []
        for address in self.target.addresses:
            try:
                return self.backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise errors[-1]
        raise UnsafeRemoteUrlError("validated target has no pinned IP addresses")

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise UnsafeRemoteUrlError("Unix socket connections are not allowed for remote URLs")


class CoreResponseStream(httpx.SyncByteStream):
    def __init__(self, response):
        self.response = response

    def __iter__(self):
        yield from self.response.stream

    def close(self):
        self.response.close()


class PinnedHTTPTransport(httpx.BaseTransport):
    """HTTPX transport backed by httpcore and an IP-pinned network backend."""

    def __init__(self, target: ValidatedRemoteTarget):
        self.pool = httpcore.ConnectionPool(
            ssl_context=ssl.create_default_context(),
            max_connections=5,
            max_keepalive_connections=0,
            network_backend=PinnedNetworkBackend(target),
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        response = self.pool.handle_request(core_request)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=CoreResponseStream(response),
            extensions=response.extensions,
            request=request,
        )

    def close(self):
        self.pool.close()


def make_slug(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-zA-Z0-9\s-]", "", text.lower())
    slug = re.sub(r"\s+", "-", slug).strip("-")
    return slug[:max_len]


def _fetch_validated_bytes(url: str) -> tuple[str, str, str, bytes] | None:
    """Fetch raw bytes from a DNS-pinned public HTTP(S) URL without redirects or proxy leakage."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        target = validate_remote_url(url)
        checked_url = target.url
        print(f"  [Fetching] {checked_url}")
        transport = PinnedHTTPTransport(target)
        with httpx.Client(
            transport=transport,
            follow_redirects=False,
            timeout=httpx.Timeout(30.0, connect=10.0),
            trust_env=False,
        ) as client:
            with client.stream("GET", checked_url, headers=headers) as response:
                if 300 <= response.status_code < 400:
                    print("  [!] Redirect refused; add the final public URL explicitly to the reading list.")
                    return None
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                declared_length = response.headers.get("content-length")
                if declared_length and int(declared_length) > MAX_RESPONSE_BYTES:
                    print("  [!] Response exceeds the 5 MiB Scout limit.")
                    return None
                chunks = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        print("  [!] Response exceeded the 5 MiB Scout limit.")
                        return None
                    chunks.append(chunk)
                raw_body = b"".join(chunks)
                encoding = response.encoding or "utf-8"
                return checked_url, content_type, encoding, raw_body
    except Exception as e:
        print(f"  [!] Fetch failed: {e}")
        return None


def parse_feed_links(xml_text: str, *, max_items: int = 15) -> list[str]:
    """Extract item/entry HTTP(S) URLs from RSS 2.0 or Atom XML while rejecting DTD/XXE constructs."""
    upper_head = xml_text[:8192].upper()
    if "<!DOCTYPE" in upper_head or "<!ENTITY" in upper_head:
        raise ValueError("Refusing XML feed containing DOCTYPE or ENTITY declarations.")
    try:
        root = ET.fromstring(xml_text.lstrip())
    except ET.ParseError as exc:
        raise ValueError(f"Invalid RSS/Atom XML: {exc}") from exc

    def _local_name(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].lower() if "}" in tag else tag.lower()

    urls: list[str] = []
    seen: set[str] = set()
    for elem in root.iter():
        lname = _local_name(elem.tag)
        if lname not in {"item", "entry"}:
            continue
        candidate = ""
        for child in elem:
            cname = _local_name(child.tag)
            if cname != "link":
                continue
            href = (child.attrib.get("href") or "").strip()
            rel = (child.attrib.get("rel") or "alternate").strip().lower()
            text_val = (child.text or "").strip()
            if href and rel in {"alternate", ""}:
                candidate = href
                break
            if text_val and not candidate:
                candidate = text_val
        if candidate.startswith(("http://", "https://")) and candidate not in seen:
            seen.add(candidate)
            urls.append(candidate)
            if len(urls) >= max_items:
                break
    return urls


def fetch_feed(url: str, *, max_items: int = 15) -> list[str] | None:
    """Fetch and parse an RSS/Atom feed over the SSRF-pinned transport."""
    fetched = _fetch_validated_bytes(url)
    if fetched is None:
        return None
    _, _, encoding, raw_body = fetched
    xml_text = raw_body.decode(encoding, errors="replace")
    try:
        return parse_feed_links(xml_text, max_items=max_items)
    except ValueError as exc:
        print(f"  [!] Feed parse failed: {exc}")
        return None


def fetch_article(url: str) -> dict | None:
    """Fetch and extract full article text (or detect RSS/Atom XML) from a URL."""
    fetched = _fetch_validated_bytes(url)
    if fetched is None:
        return None
    checked_url, content_type, encoding, raw_body = fetched
    xml_media_types = ("application/rss+xml", "application/atom+xml", "application/xml", "text/xml")
    if any(kind in content_type for kind in xml_media_types):
        xml_text = raw_body.decode(encoding, errors="replace")
        try:
            feed_urls = parse_feed_links(xml_text)
            return {"kind": "feed", "url": checked_url, "feed_urls": feed_urls}
        except ValueError as exc:
            print(f"  [!] Feed XML rejected: {exc}")
            return None
    if content_type and not any(kind in content_type for kind in ("text/html", "application/xhtml+xml")):
        print(f"  [!] Unsupported content type: {content_type}")
        return None
    html = raw_body.decode(encoding, errors="replace")

    title = "Unknown"
    content = ""
    word_count = 0

    if HAS_TRAFILATURA:
        # Best-in-class article extraction
        newconfig = use_config()
        newconfig.set("DEFAULT", "EXTRACTION_TIMEOUT", "30")
        extracted = trafilatura.extract(
            html,
            url=checked_url,
            include_comments=False,
            include_tables=True,
            no_fallback=False,
            config=newconfig,
        )
        meta = trafilatura.extract_metadata(html, default_url=url)
        if meta:
            title = meta.title or "Unknown"
        content = extracted or ""
    elif HAS_BS4:
        # Fallback to BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else "Unknown"
        # Remove script/style
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        content = soup.get_text(separator="\n", strip=True)
    else:
        print("  [!] No extraction library available.")
        return None

    if not content or len(content) < 100:
        print(f"  [!] Content too short or empty — possible bot-block.")
        return None

    word_count = len(content.split())

    return {
        "url":       checked_url,
        "title":     title,
        "content":   content,
        "word_count": word_count,
        "raw_body": raw_body,
        "response_sha256": hashlib.sha256(raw_body).hexdigest(),
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def save_source(article: dict) -> str:
    """Save article to sources/ and return filename."""
    if _is_reparse_or_link(SOURCES_DIR):
        raise PermissionError(f"Refusing to write into symlinked sources directory: {SOURCES_DIR}")
    slug = make_slug(article["title"]) or "web-source"
    url_hash = hashlib.sha256(article["url"].encode("utf-8")).hexdigest()[:12]
    base_stem = f"{today()}-{slug}-{url_hash}"
    evidence_dir = os.path.join(SOURCES_DIR, "evidence")
    evidence_filename = f"{article['response_sha256']}.html"
    evidence_path = os.path.join(evidence_dir, evidence_filename)

    os.makedirs(evidence_dir, exist_ok=True)
    if _is_reparse_or_link(evidence_dir):
        raise PermissionError(f"Refusing to write into symlinked evidence directory: {evidence_dir}")
    if not os.path.exists(evidence_path):
        try:
            with open(evidence_path, "xb") as evidence_file:
                evidence_file.write(article["raw_body"])
        except FileExistsError:
            pass

    frontmatter = f"""---
url: {article['url']}
title: {json.dumps(article['title'], ensure_ascii=False)}
author: Unknown
publication_date: Unknown
captured_date: {today()}
word_count: {article['word_count']}
origin: web_remote
evidence_path: sources/evidence/{evidence_filename}
response_sha256: {article['response_sha256']}
extracted_content_sha256: {article['content_sha256']}
scout_status: fetched
---

"""
    payload = (frontmatter + article["content"]).replace("\r\n", "\n")
    counter = 1
    while True:
        filename = f"{base_stem if counter == 1 else f'{base_stem}-{counter}'}.md"
        filepath = os.path.join(SOURCES_DIR, filename)
        if os.path.isfile(filepath) and not _is_reparse_or_link(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as existing:
                    head = existing.read(2048)
                if f"response_sha256: {article['response_sha256']}" in head:
                    return filename
            except OSError:
                pass
        try:
            with open(filepath, "x", encoding="utf-8", newline="\n") as f:
                f.write(payload)
            break
        except FileExistsError:
            counter += 1

    print(f"  [✓] Saved → sources/{filename}  ({article['word_count']} words)")
    return filename


def create_intake_pointer(source_filename: str, raw_dir: str = RAW_DIR) -> str:
    """Create a quarantined pointer that cannot enter the Refinery without owner promotion."""
    if os.path.lexists(raw_dir) and _is_reparse_or_link(raw_dir):
        raise PermissionError(f"Refusing to write intake pointer into symlinked raw directory: {raw_dir}")
    pointer_name = source_filename.replace(".md", ".intake")
    pointer_path = os.path.join(raw_dir, pointer_name)
    try:
        with open(pointer_path, "x", encoding="utf-8", newline="\n") as f:
            f.write(
                f"source: sources/{source_filename}\n"
                "origin: web_remote\n"
                "intake_status: quarantined\n"
                "requires_owner_promotion: true\n"
            )
    except FileExistsError:
        # Existing pointers may have an owner decision; never reset or replace them.
        return pointer_path
    print(f"  [quarantined] Intake pointer: 0-raw/{pointer_name}")
    return pointer_path


# ─── Reading List Parser ──────────────────────────────────────────────────────

def parse_reading_list() -> list[dict]:
    """Return list of uncompleted URL and RSS/Atom feed entries from reading-list.md."""
    if not os.path.exists(READING_LIST):
        print("[!] reading-list.md not found.")
        return []
    if _is_reparse_or_link(READING_LIST):
        raise PermissionError(f"Refusing to read symlinked reading list: {READING_LIST}")

    with open(READING_LIST, "r", encoding="utf-8") as f:
        content = f.read()

    entries: list[dict] = []
    for line in content.splitlines():
        feed_persist = re.match(r"^\s*-\s*\[feed\]\s+(https?://\S+)", line, re.IGNORECASE)
        if feed_persist:
            entries.append({"url": feed_persist.group(1), "kind": "feed_persistent"})
            continue
        feed_once = re.match(r"^\s*-\s*\[\s\]\s+feed:\s*(https?://\S+)", line, re.IGNORECASE)
        if feed_once:
            entries.append({"url": feed_once.group(1), "kind": "feed_once"})
            continue
        article_match = re.match(r"^\s*-\s*\[\s\]\s+(https?://\S+)", line)
        if article_match:
            entries.append({"url": article_match.group(1)})
    return entries


def enqueue_discovered_urls(urls: list[str], *, max_new: int = 10) -> list[str]:
    """Append newly discovered RSS/Atom item URLs as uncompleted reading-list items."""
    if not urls or not os.path.exists(READING_LIST):
        return []
    with review_lock(os.path.join(SIDECAR_DIR, "writer.lock")):
        if _is_reparse_or_link(READING_LIST):
            raise PermissionError(f"Refusing to modify symlinked reading list: {READING_LIST}")
        with open(READING_LIST, "r", encoding="utf-8") as f:
            content = f.read().replace("\r\n", "\n")
        added: list[str] = []
        for candidate in urls:
            clean = str(candidate or "").strip()
            if not clean.startswith(("http://", "https://")):
                continue
            if re.search(rf"(?m)^\s*-\s*\[[^\]]+\]\s+(?:feed:\s*)?{re.escape(clean)}(?=\s|$)", content):
                continue
            added.append(clean)
            if len(added) >= max_new:
                break
        if added:
            suffix = "" if content.endswith("\n") or not content else "\n"
            appended = "\n".join(f"- [ ] {u}" for u in added) + "\n"
            atomic_write_bytes(READING_LIST, (content + suffix + appended).encode("utf-8"))
        return added


def update_reading_list(url: str, status: str, note: str = ""):
    """Mark a URL as [x] (fetched) or [!] (failed) in reading-list.md without regex escape/prefix hazards."""
    with review_lock(os.path.join(SIDECAR_DIR, "writer.lock")):
        if _is_reparse_or_link(READING_LIST):
            raise PermissionError(f"Refusing to modify symlinked reading list: {READING_LIST}")
        with open(READING_LIST, "r", encoding="utf-8") as f:
            content = f.read().replace("\r\n", "\n")

        marker = "[x]" if status == "ok" else "[!]"
        clean_note = str(note or "").replace("\r", " ").replace("\n", " ").strip()
        note_line = f"\n  ↳ {clean_note}" if clean_note else ""
        pattern = re.compile(
            rf"^(\s*)-\s*\[\s\]\s+(?:feed:\s*)?({re.escape(url)})(?=\s|$)",
            re.MULTILINE,
        )
        updated = pattern.sub(
            lambda m: f"{m.group(1)}- {marker} {m.group(2)}{note_line}",
            content,
            count=1,
        )
        atomic_write_bytes(READING_LIST, updated.encode("utf-8"))


mark_url_status = update_reading_list


# ─── Scout Run ────────────────────────────────────────────────────────────────

def run_scout():
    ensure_dirs()
    initial_entries = parse_reading_list()

    if not initial_entries:
        print("[*] No URLs queued in reading-list.md.")
        return

    fetched, failed = 0, 0
    feed_entries = [e for e in initial_entries if e.get("kind") in {"feed_persistent", "feed_once"}]
    for entry in feed_entries:
        url = entry["url"]
        print(f"[→ Feed] {url}")
        discovered = fetch_feed(url)
        if discovered is not None:
            added = enqueue_discovered_urls(discovered)
            print(f"  [✓] Feed polled: {len(added)} new item(s) queued.")
            if entry.get("kind") == "feed_once":
                update_reading_list(url, "ok", f"Feed polled — queued {len(added)} item(s)")
        else:
            if entry.get("kind") == "feed_once":
                update_reading_list(url, "fail", "Feed fetch failed")
            failed += 1
        print()

    entries = [e for e in parse_reading_list() if e.get("kind") not in {"feed_persistent", "feed_once"}]
    if not entries and not feed_entries:
        print("[*] No URLs queued in reading-list.md.")
        return

    if entries:
        print(f"[*] Scout found {len(entries)} URL(s) to fetch.\n")

    for entry in entries:
        url = entry["url"]
        print(f"[→] {url}")
        article = fetch_article(url)

        if article and article.get("kind") == "feed":
            added = enqueue_discovered_urls(article.get("feed_urls") or [])
            update_reading_list(url, "ok", f"Feed XML detected — queued {len(added)} item(s)")
            fetched += 1
        elif article:
            try:
                source_file = save_source(article)
                create_intake_pointer(source_file)
                update_reading_list(
                    url,
                    "ok",
                    f"Saved: sources/{source_file} — {article['word_count']} words",
                )
                fetched += 1
            except Exception as exc:
                print(f"  [!] Saving source failed: {exc}")
                update_reading_list(url, "fail", f"Save error — {exc}")
                failed += 1
        else:
            update_reading_list(url, "fail", "Failed — manual fetch needed")
            failed += 1

        print()

    # Write scout log to configured BRIEFINGS_DIR
    os.makedirs(BRIEFINGS_DIR, exist_ok=True)
    if not _is_reparse_or_link(BRIEFINGS_DIR):
        log_path = os.path.join(BRIEFINGS_DIR, f"scout-log-{today()}.md")
        suffix = 1
        base, ext = os.path.splitext(log_path)
        while os.path.exists(log_path):
            log_path = f"{base}-{suffix}{ext}"
            suffix += 1
        with open(log_path, "x", encoding="utf-8", newline="\n") as f:
            f.write(
                f"# Scout Log — {today()}\n\n"
                f"Fetched: {fetched}  |  Failed: {failed}\n"
            )

    print(f"[*] Scout complete. Fetched: {fetched}, Failed: {failed}")


if __name__ == "__main__":
    print("=== WEB SCOUT ===")
    run_scout()
    print("=== DONE ===")
