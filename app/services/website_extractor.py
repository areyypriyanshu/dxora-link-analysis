"""Fetch a public web page and extract the parts an LLM can work with."""

import asyncio
import re

import anyio
import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from ..config import Settings, get_settings
from ..utils.validators import UnsafeURLError, is_public_ip, validate_public_url

REDIRECT_STATUSES = {301, 302, 303, 307, 308}
NOISE_TAGS = ("script", "style", "noscript", "template", "svg", "iframe", "form", "button")
_WHITESPACE = re.compile(r"[ \t\u00a0]+")


class FetchError(RuntimeError):
    """The target website could not be fetched or contained no usable content."""


class ExtractedContent(BaseModel):
    url: str
    final_url: str
    title: str | None = None
    meta_description: str | None = None
    site_name: str | None = None
    headings: list[str] = Field(default_factory=list)
    nav_links: list[str] = Field(default_factory=list)
    text: str = ""


def _clean(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def _peer_address(response: httpx.Response) -> str | None:
    stream = response.extensions.get("network_stream")
    if stream is None:
        return None
    address = stream.get_extra_info("server_addr")
    if isinstance(address, tuple) and address:
        return str(address[0])
    return None


def _ensure_public_peer(response: httpx.Response) -> None:
    """Reject the response if the TCP connection landed on a non-global address."""
    peer = _peer_address(response)
    if peer is not None and not is_public_ip(peer):
        raise UnsafeURLError("Website connected to a private or internal network address")


async def fetch_html(url: str, client: httpx.AsyncClient) -> tuple[str, str]:
    """Download the page under one total deadline, validating every hop.

    Returns (final_url, html).
    """
    settings = get_settings()
    try:
        async with asyncio.timeout(settings.fetch_timeout_seconds):
            return await _fetch_html(url, client, settings)
    except (TimeoutError, httpx.TimeoutException) as exc:
        raise FetchError("Timed out while fetching the website") from exc


async def _fetch_html(url: str, client: httpx.AsyncClient, settings: Settings) -> tuple[str, str]:
    """Download the page, validating every redirect hop against the SSRF rules."""
    current = url

    for _ in range(settings.max_redirects + 1):
        await anyio.to_thread.run_sync(validate_public_url, current, abandon_on_cancel=True)
        try:
            async with client.stream(
                "GET",
                current,
                timeout=settings.fetch_timeout_seconds,
                follow_redirects=False,
            ) as response:
                _ensure_public_peer(response)
                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError("Website returned a redirect without a Location header")
                    current = str(httpx.URL(current).join(location))
                    continue

                if response.status_code >= 400:
                    raise FetchError(f"Website returned HTTP {response.status_code}")

                content_type = response.headers.get("content-type", "")
                if "html" not in content_type.lower():
                    raise FetchError(
                        f"Unsupported content type '{content_type or 'unknown'}'; expected an HTML page"
                    )

                declared_size = response.headers.get("content-length")
                if declared_size and declared_size.isdigit() and int(declared_size) > settings.max_page_bytes:
                    raise FetchError("Website page is too large to analyse")

                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) >= settings.max_page_bytes:
                        break

                encoding = response.charset_encoding or "utf-8"
                return str(response.url), bytes(buffer).decode(encoding, errors="replace")

        except httpx.TimeoutException as exc:
            raise FetchError("Timed out while fetching the website") from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"Could not fetch the website: {exc}") from exc

    raise FetchError("Website redirected too many times")


def extract_content(html: str, url: str, final_url: str) -> ExtractedContent:
    settings = get_settings()
    soup = BeautifulSoup(html, "lxml")

    meta: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        content = (tag.get("content") or "").strip()
        key = (tag.get("name") or tag.get("property") or "").lower()
        if key and content and key not in meta:
            meta[key] = content

    title = _clean(soup.title.get_text(" ")) if soup.title else None

    for tag in soup(NOISE_TAGS):
        tag.decompose()

    headings: list[str] = []
    for tag in soup.find_all(["h1", "h2"]):
        text = _clean(tag.get_text(" "))
        if text and text not in headings:
            headings.append(text)

    nav_links: list[str] = []
    for anchor in soup.select("header a, nav a"):
        text = _clean(anchor.get_text(" "))
        if text and len(text) <= 60 and text not in nav_links:
            nav_links.append(text)

    body = soup.find("main") or soup.body or soup
    lines = []
    for line in body.get_text("\n").splitlines():
        cleaned = _clean(line)
        if len(cleaned) > 2:
            lines.append(cleaned)

    text = "\n".join(dict.fromkeys(lines))

    return ExtractedContent(
        url=url,
        final_url=final_url,
        title=title,
        meta_description=meta.get("description") or meta.get("og:description"),
        site_name=meta.get("og:site_name") or meta.get("application-name"),
        headings=headings[:25],
        nav_links=nav_links[:30],
        text=text[: settings.max_content_chars],
    )


def build_prompt_document(content: ExtractedContent) -> str:
    parts = [
        f"Page URL: {content.final_url}",
        f"Page title: {content.title or 'N/A'}",
        f"Site name: {content.site_name or 'N/A'}",
        f"Meta description: {content.meta_description or 'N/A'}",
        "Headings:\n" + ("\n".join(f"- {h}" for h in content.headings) or "- N/A"),
        "Navigation / section links:\n" + ("\n".join(f"- {l}" for l in content.nav_links) or "- N/A"),
        "Page text:\n" + (content.text or "N/A"),
    ]
    return "\n\n".join(parts)
