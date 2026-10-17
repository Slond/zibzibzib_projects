"""Extract allowlisted exercise videos without executing page scripts."""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup


def video_url(value, base="https://www.muscleandstrength.com"):
    """Normalize player URLs, keeping Vimeo's unlisted-video access hash."""
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parts = urlsplit(urljoin(base, value.strip()))
        if (parts.scheme not in {"http", "https"} or parts.username or parts.password
                or parts.port not in {None, 80, 443}):
            return ""
        query = parse_qs(parts.query)
        host = parts.hostname
        identifier = ""
        if host in {"youtube.com", "www.youtube.com", "www.youtube-nocookie.com"}:
            match = re.fullmatch(r"/(?:embed|shorts)/([\w-]{11})/?", parts.path)
            identifier = match[1] if match else query.get("v", [""])[0] if parts.path == "/watch" else ""
        elif host == "youtu.be":
            identifier = parts.path.strip("/")
        if re.fullmatch(r"[a-zA-Z0-9_-]{11}", identifier):
            return "https://www.youtube.com/embed/" + identifier
        if host in {"vimeo.com", "www.vimeo.com", "player.vimeo.com"}:
            match = re.fullmatch(r"/(?:video/)?(\d+)(?:/([a-zA-Z0-9]+))?/?", parts.path)
            if match:
                access = query.get("h", [match[2] or ""])[0]
                if access and not re.fullmatch(r"[a-zA-Z0-9]+", access):
                    return ""
                return "https://player.vimeo.com/video/" + match[1] + ("?" + urlencode({"h": access}) if access else "")
    except (ValueError, TypeError):
        pass
    return ""


def extract_video(soup: BeautifulSoup, base):
    # The article player is authoritative; ignore recommendations, navigation and comments.
    article = soup.select_one("main .node-header") or soup.select_one("main") or soup
    for frame in article.select("iframe"):
        if frame.find_parent(["nav", "footer"]) or frame.find_parent(class_=re.compile(r"comment|related", re.I)):
            continue
        for attribute in ("data-src", "src"):
            found = video_url(frame.get(attribute, ""), base)
            if found:
                return found

    def objects(value):
        if isinstance(value, list):
            for item in value:
                yield from objects(item)
        elif isinstance(value, dict):
            yield value
            for key in ("@graph", "video", "mainEntity"):
                yield from objects(value.get(key))

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            document = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        for item in objects(document):
            kinds = item.get("@type", [])
            if "VideoObject" not in (kinds if isinstance(kinds, list) else [kinds]):
                continue
            for key in ("embedUrl", "contentUrl", "url"):
                found = video_url(item.get(key), base)
                if found:
                    return found
    return ""
