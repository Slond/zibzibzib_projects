#!/usr/bin/env python3
"""Collect exercise links from Muscle & Strength category listings, without fetching each exercise.

The checkpoint allows interrupted runs to resume. The app database is never opened.
"""
from __future__ import annotations

import argparse
from collections import deque
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup
import httpx

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "https://www.muscleandstrength.com/exercises"
ORIGIN = "https://www.muscleandstrength.com"
USER_AGENT = "PersonalWorkoutJournal/1.0"
HOSTS = {"www.muscleandstrength.com", "muscleandstrength.com"}
MAX_RESPONSE = 4 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value, base=SOURCE, *, pagination=False):
    """Keep only the exercise namespace, normalizing aliases/tracking and page zero."""
    parts = urlsplit(urljoin(base, value))
    if (parts.scheme not in {"http", "https"} or parts.hostname not in HOSTS
            or parts.username or parts.password or parts.port not in {None, 80, 443}):
        raise ValueError("Link is outside Muscle & Strength")
    path = parts.path.rstrip("/")
    if not re.fullmatch(r"/exercises(?:/[a-zA-Z0-9_-]+(?:\.html)?)?", path):
        raise ValueError("Link is outside the exercise catalog")
    query = ""
    if pagination and parts.query:
        values = parse_qs(parts.query)
        if set(values) != {"page"} or len(values["page"]) != 1 or not values["page"][0].isdecimal():
            raise ValueError("Only unfiltered pagination is followed")
        page = int(values["page"][0])
        if not 0 <= page <= 10000:
            raise ValueError("Page number is out of bounds")
        query = "?" + urlencode({"page": page}) if page else ""
    return ORIGIN + path + query


def discover_categories(html):
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main")
    if not main:
        raise ValueError("Catalog layout not recognized: no main element")
    categories = {}
    for block in main.select(".exercise-category-list"):
        heading = block.find_previous("h2")
        heading = heading.get_text(" ", strip=True).lower() if heading else ""
        kind = "muscle" if "muscle" in heading else "equipment" if "equipment" in heading else "mechanics" if "mechanics" in heading else "category"
        for anchor in block.select("a[href]"):
            name = anchor.get_text(" ", strip=True)
            if not name:
                continue
            try:
                url = canonical(anchor["href"])
            except ValueError:
                continue
            if url != SOURCE:
                categories[url] = {"url": url, "name_en": name, "kind": kind}
    if not categories:
        raise ValueError("Catalog layout not recognized: no exercise categories")
    return list(categories.values())


def parse_listing(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main")
    if not main:
        raise ValueError("Listing layout not recognized: no main element")
    exercises = {}
    for anchor in main.select(".node-title a[href]"):
        name = anchor.get_text(" ", strip=True)
        if not name:
            continue
        try:
            url = canonical(anchor["href"], page_url)
        except ValueError:
            continue
        card = anchor.parent.parent
        metadata = {}
        for box in card.select(".exercise-meta .meta-box"):
            label = box.find("label")
            if label:
                key = label.get_text(" ", strip=True)
                value = box.get_text(" ", strip=True).removeprefix(key).strip()
                metadata[key] = value
        image = card.select_one(".node-image img")
        preview = urljoin(page_url, image.get("data-src") or image.get("src") or "") if image else ""
        record = {"name_en": name, "url": url, "equipment": metadata.get("Equipment", ""),
                  "mechanics": metadata.get("Mechanics", ""), "experience_level": metadata.get("Exp. Level", ""),
                  "exercise_type": metadata.get("Type", ""), "preview_url": preview}
        # Featured cards may repeat in the full listing with richer metadata.
        if url not in exercises or metadata:
            exercises[url] = record
    if not exercises:
        raise ValueError("Listing has no exercise cards; refusing to mark it complete")
    pagination = set()
    for anchor in main.select(".pager a[href]"):
        try:
            target = canonical(anchor["href"], page_url, pagination=True)
        except ValueError:
            continue
        if urlsplit(target).path == urlsplit(page_url).path:
            pagination.add(target)
    return list(exercises.values()), sorted(pagination)


def atomic_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


class CrawlStopped(Exception):
    pass


class Collector:
    def __init__(self, output, *, delay=1.5, max_pages=None, refresh=False, client=None, translations=None):
        self.output = Path(output)
        translation_file = Path(translations) if translations else self.output / "translations.ru.json"
        self.translations = json.loads(translation_file.read_text()) if translation_file.exists() else {}
        if not isinstance(self.translations, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in self.translations.items()):
            raise ValueError("Translations must be a JSON object: exercise URL or id -> Russian name")
        self.delay = max(1.0, delay)
        self.max_pages = max_pages
        self.last_request = 0.0
        self.requests = 0
        self.error = ""
        self.robots = None
        self.client = client or httpx.Client(timeout=25, follow_redirects=False, headers={"User-Agent": USER_AGENT})
        self.owns_client = client is None
        checkpoint = self.output / "checkpoint.json"
        self.state = json.loads(checkpoint.read_text()) if checkpoint.exists() and not refresh else {
            "version": 1, "source": SOURCE, "categories": [], "pages": {}, "errors": {}, "started_at": now()}
        if self.state.get("version") != 1 or self.state.get("source") != SOURCE:
            raise ValueError("Incompatible checkpoint; use another output directory")
        self.complete = False
        self.pending = []

    def fetch(self, url, *, robots=False):
        if not robots and self.robots and not self.robots.can_fetch(USER_AGENT, url):
            raise CrawlStopped("robots.txt disallows: " + url)
        for attempt in range(3):
            try:
                for _ in range(4):
                    if not robots and self.robots and not self.robots.can_fetch(USER_AGENT, url):
                        raise CrawlStopped("robots.txt disallows redirected URL: " + url)
                    time.sleep(max(0.0, self.last_request + self.delay - time.monotonic()))
                    self.last_request = time.monotonic()
                    with self.client.stream("GET", url) as response:
                        if response.status_code in {403, 429}:
                            raise CrawlStopped(f"HTTP {response.status_code}: source blocked requests; saved progress, no bypass attempted")
                        if response.status_code in {301, 302, 303, 307, 308}:
                            if robots:
                                raise CrawlStopped("robots.txt redirected; please check the source manually")
                            url = canonical(response.headers.get("location", ""), url, pagination=True)
                            continue
                        if robots and response.status_code == 404:
                            return ""
                        response.raise_for_status()
                        body = bytearray()
                        for chunk in response.iter_bytes():
                            body.extend(chunk)
                            if len(body) > MAX_RESPONSE:
                                raise CrawlStopped("Source response exceeds size limit")
                        html = body.decode("utf-8", errors="replace")
                        if not robots and re.search(r"<title[^>]*>\s*(?:Just a moment|Attention Required|Access Denied)", html, re.I):
                            raise CrawlStopped("Source returned a challenge page; saved progress")
                        return html
                raise CrawlStopped("Too many redirects")
            except (httpx.TimeoutException, httpx.NetworkError) as error:
                if attempt == 2:
                    raise CrawlStopped(type(error).__name__ + ": network unavailable") from error
            except httpx.HTTPStatusError as error:
                if error.response.status_code < 500 or attempt == 2:
                    raise CrawlStopped(f"HTTP {error.response.status_code}: {url}") from error
            time.sleep(2 ** attempt)
        raise AssertionError("Unreachable")

    def export(self):
        categories = {c["url"]: {**c, "pages": 0, "exercise_urls": set()} for c in self.state["categories"]}
        records = {}
        for page in self.state["pages"].values():
            category = categories[page["category"]]
            category["pages"] += 1
            for item in page["exercises"]:
                category["exercise_urls"].add(item["url"])
                record = records.setdefault(item["url"], {
                    "id": "ms_" + hashlib.sha256(item["url"].encode()).hexdigest()[:16],
                    "name_en": item["name_en"], "name_ru": "", "url": item["url"],
                    "categories": [], "muscle_groups": [], "equipment": [], "mechanics": [],
                    "experience_levels": [], "exercise_types": [], "preview_url": item["preview_url"],
                })
                record["categories"].append(category["name_en"])
                if category["kind"] == "muscle":
                    record["muscle_groups"].append(category["name_en"])
                for source, target in [("equipment", "equipment"), ("mechanics", "mechanics"),
                                       ("experience_level", "experience_levels"), ("exercise_type", "exercise_types")]:
                    if item[source]:
                        record[target].append(item[source])
        rows = sorted(records.values(), key=lambda r: (r["name_en"].casefold(), r["url"]))
        for record in rows:
            record["name_ru"] = self.translations.get(record["url"], self.translations.get(record["id"], ""))
            for key in ["categories", "muscle_groups", "equipment", "mechanics", "experience_levels", "exercise_types"]:
                record[key] = sorted(set(record[key]))
        folders = [{**{k: v for k, v in c.items() if k != "exercise_urls"}, "exercises": len(c["exercise_urls"])} for c in categories.values()]
        summary = {"source": SOURCE, "updated_at": now(), "complete": self.complete,
                   "categories": len(folders), "listing_pages": len(self.state["pages"]),
                   "unique_exercises": len(rows), "pending_pages": self.pending, "errors": self.state["errors"],
                   "scope": "Categories linked from the source index and all discovered listing pagination",
                   "individual_exercise_pages_fetched": False,
                   "missing_metadata": {key: sum(not row[key] for row in rows)
                                        for key in ["muscle_groups", "equipment", "mechanics", "experience_levels", "preview_url"]}}
        atomic_text(self.output / "checkpoint.json", json.dumps(self.state, ensure_ascii=False, indent=2) + "\n")
        atomic_text(self.output / "categories.json", json.dumps(folders, ensure_ascii=False, indent=2) + "\n")
        atomic_text(self.output / "exercises.json", json.dumps({"summary": summary, "exercises": rows}, ensure_ascii=False, indent=2) + "\n")
        atomic_text(self.output / "summary.json", json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        atomic_text(self.output / "exercise_urls.txt", "".join(r["url"] + "\n" for r in rows))
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=["name_en", "name_ru", "url", "muscle_groups", "equipment", "mechanics", "experience_levels", "exercise_types", "categories", "preview_url"])
        writer.writeheader()
        for row in rows:
            values = {key: " | ".join(row[key]) if isinstance(row[key], list) else row[key] for key in writer.fieldnames}
            # Imported names are untrusted text; keep spreadsheet formulas inert.
            writer.writerow({key: "\'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value for key, value in values.items()})
        atomic_text(self.output / "exercises.csv", "\ufeff" + buffer.getvalue())
        return summary

    def run(self):
        queue = deque()
        try:
            rules = self.fetch(ORIGIN + "/robots.txt", robots=True)
            self.robots = RobotFileParser()
            self.robots.parse(rules.splitlines())
            self.delay = max(self.delay, self.robots.crawl_delay(USER_AGENT) or 0)
            if not self.state["categories"]:
                self.state["categories"] = discover_categories(self.fetch(SOURCE))
            self.state["errors"].pop(SOURCE, None)
            queue.extend((c["url"], c["url"]) for c in self.state["categories"])
            queued = {url for url, _ in queue}
            seen = set()
            while queue:
                url, category = queue[0]
                if url in seen:
                    queue.popleft()
                    continue
                is_new = url not in self.state["pages"]
                if is_new:
                    if self.max_pages is not None and self.requests >= self.max_pages:
                        break
                    html = self.fetch(url)
                    exercises, links = parse_listing(html, url)
                    self.state["pages"][url] = {"category": category, "fetched_at": now(), "exercises": exercises, "pagination": links}
                    self.state["errors"].pop(url, None)
                    self.requests += 1
                    print(f'[{len(self.state["pages"]):3}] {url}: {len(exercises)} exercises', flush=True)
                page = self.state["pages"][url]
                for link in page["pagination"]:
                    if link not in queued:
                        queue.append((link, category))
                        queued.add(link)
                queue.popleft()
                seen.add(url)
                self.pending = [item[0] for item in queue]
                if is_new:
                    self.export()
            self.complete = not queue
        except (CrawlStopped, ValueError, httpx.HTTPError) as error:
            self.error = str(error)
            self.state["errors"][queue[0][0] if queue else SOURCE] = {"message": self.error, "at": now()}
        except KeyboardInterrupt:
            self.error = "Interrupted; rerun the same command to resume"
        finally:
            self.pending = [item[0] for item in queue]
            result = self.export()
            if self.owns_client:
                self.client.close()
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        if self.error:
            print(self.error, file=sys.stderr)
        return 0 if self.complete else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data/exercise_catalog/muscle_strength")
    parser.add_argument("--delay", type=float, default=1.5, help="Minimum seconds between requests (at least 1)")
    parser.add_argument("--max-pages", type=int, help="Fetch at most this many new listing pages; rerun to resume")
    parser.add_argument("--translations", type=Path, help="JSON mapping exercise URL or stable id to Russian name")
    parser.add_argument("--refresh", action="store_true", help="Start a fresh scan instead of reusing the checkpoint")
    args = parser.parse_args()
    if not 1 <= args.delay <= 60:
        parser.error("--delay must be between 1 and 60 seconds")
    if args.max_pages is not None and args.max_pages < 1:
        parser.error("--max-pages must be positive")
    return Collector(args.output, delay=args.delay, max_pages=args.max_pages, refresh=args.refresh, translations=args.translations).run()


if __name__ == "__main__":
    raise SystemExit(main())
