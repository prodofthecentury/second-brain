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
import httpx
import httpcore

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import (
    READING_LIST, SOURCES_DIR, RAW_DIR, SIDECAR_DIR,
    today, ensure_dirs
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
        address = ipaddress.ip_address(answer[4][0])
        if not address.is_global:
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


def fetch_article(url: str) -> dict | None:
    """Fetch and extract full article text from a URL."""
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
                if content_type and not any(kind in content_type for kind in ("text/html", "application/xhtml+xml")):
                    print(f"  [!] Unsupported content type: {content_type}")
                    return None
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
                html = raw_body.decode(response.encoding or "utf-8", errors="replace")
    except Exception as e:
        print(f"  [!] Fetch failed: {e}")
        return None

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
    slug     = make_slug(article["title"]) or "web-source"
    url_hash = hashlib.sha256(article["url"].encode("utf-8")).hexdigest()[:12]
    filename = f"{today()}-{slug}-{url_hash}.md"
    filepath = os.path.join(SOURCES_DIR, filename)
    evidence_dir = os.path.join(SOURCES_DIR, "evidence")
    evidence_filename = f"{article['response_sha256']}.html"
    evidence_path = os.path.join(evidence_dir, evidence_filename)

    os.makedirs(evidence_dir, exist_ok=True)
    if not os.path.exists(evidence_path):
        with open(evidence_path, "xb") as evidence_file:
            evidence_file.write(article["raw_body"])
    if os.path.exists(filepath):
        raise RuntimeError(f"Refusing to overwrite existing external source: {filename}")

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
    with open(filepath, "x", encoding="utf-8") as f:
        f.write(frontmatter + article["content"])

    print(f"  [✓] Saved → sources/{filename}  ({article['word_count']} words)")
    return filename


def create_intake_pointer(source_filename: str, raw_dir: str = RAW_DIR) -> str:
    """Create a quarantined pointer that cannot enter the Refinery without owner promotion."""
    pointer_name = source_filename.replace(".md", ".intake")
    pointer_path = os.path.join(raw_dir, pointer_name)
    try:
        with open(pointer_path, "x", encoding="utf-8") as f:
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
    """Return list of uncompleted entries from reading-list.md."""
    if not os.path.exists(READING_LIST):
        print("[!] reading-list.md not found.")
        return []

    with open(READING_LIST, "r", encoding="utf-8") as f:
        content = f.read()

    # Find lines like: - [ ] https://...
    pattern = re.compile(r"^- \[ \] (https?://\S+)", re.MULTILINE)
    matches = pattern.findall(content)
    return [{"url": url} for url in matches]


def update_reading_list(url: str, status: str, note: str = ""):
    """Mark a URL as [x] (fetched) or [!] (failed) in reading-list.md."""
    with review_lock(os.path.join(SIDECAR_DIR, "writer.lock")):
        with open(READING_LIST, "r", encoding="utf-8") as f:
            content = f.read()

        marker = "[x]" if status == "ok" else "[!]"
        note_line = f"\n  ↳ {note}" if note else ""
        escaped = re.escape(url)
        content = re.sub(
            rf"- \[ \] ({escaped})",
            f"- {marker} \\1{note_line}",
            content
        )
        atomic_write_bytes(READING_LIST, content.encode("utf-8"))


# ─── Scout Run ────────────────────────────────────────────────────────────────

def run_scout():
    ensure_dirs()
    entries = parse_reading_list()

    if not entries:
        print("[*] No URLs queued in reading-list.md.")
        return

    print(f"[*] Scout found {len(entries)} URL(s) to fetch.\n")
    fetched, failed = 0, 0

    for entry in entries:
        url = entry["url"]
        print(f"[→] {url}")
        article = fetch_article(url)

        if article:
            source_file = save_source(article)
            create_intake_pointer(source_file)
            update_reading_list(
                url, "ok",
                f"Saved: sources/{source_file} — {article['word_count']} words"
            )
            fetched += 1
        else:
            update_reading_list(url, "fail", "Failed — manual fetch needed")
            failed += 1

        print()

    # Write scout log to briefings/
    log_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "brain", "briefings", f"scout-log-{today()}.md"
    )
    suffix = 1
    base, ext = os.path.splitext(log_path)
    while os.path.exists(log_path):
        log_path = f"{base}-{suffix}{ext}"
        suffix += 1
    with open(log_path, "x", encoding="utf-8") as f:
        f.write(
            f"# Scout Log — {today()}\n\n"
            f"Fetched: {fetched}  |  Failed: {failed}\n"
        )

    print(f"[*] Scout complete. Fetched: {fetched}, Failed: {failed}")


if __name__ == "__main__":
    print("=== WEB SCOUT ===")
    run_scout()
    print("=== DONE ===")
