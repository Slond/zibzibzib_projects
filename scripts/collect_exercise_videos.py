#!/usr/bin/env python3
"""Enrich the bundled exercise catalog with videos from its individual source pages.

Sequential requests respect robots.txt and stop on access restrictions. Cached HTML
and a checkpoint make interrupted runs resumable without refetching completed pages.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from urllib.robotparser import RobotFileParser

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.sport_media import extract_video  # noqa: E402
from scripts.collect_muscle_strength import Collector, CrawlStopped, ORIGIN, USER_AGENT, atomic_text, canonical, now  # noqa: E402


def collect(catalog, output, *, delay=1.5, client=None, max_pages=None):
    output = Path(output)
    loader = Collector(output / "http", delay=delay, client=client)
    checkpoint = output / "videos.json"
    state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {"version": 1, "pages": {}}
    if state.get("version") != 1 or not isinstance(state.get("pages"), dict):
        raise ValueError("Incompatible video checkpoint")
    urls = list(dict.fromkeys(canonical(e["sourceUrl"]) for e in catalog["exercises"].values()))
    error = ""
    processed = 0

    def save():
        pages = {url: state["pages"][url] for url in urls if url in state["pages"]}
        state["summary"] = {"updated_at": now(), "total": len(urls), "processed": len(pages),
                            "videos": sum(bool(p["videoUrl"]) for p in pages.values()),
                            "without_video": [url for url, page in pages.items() if not page["videoUrl"]],
                            "pending": [url for url in urls if url not in pages], "error": error,
                            "complete": len(pages) == len(urls) and not error}
        atomic_text(checkpoint, json.dumps(state, ensure_ascii=False, indent=2) + "\n")

    try:
        rules = loader.fetch(ORIGIN + "/robots.txt", robots=True)
        loader.robots = RobotFileParser()
        loader.robots.parse(rules.splitlines())
        loader.delay = max(loader.delay, loader.robots.crawl_delay(USER_AGENT) or 0)
        for url in urls:
            if url in state["pages"]:
                continue
            if max_pages is not None and processed >= max_pages:
                break
            cache = output / "html" / (hashlib.sha256(url.encode()).hexdigest() + ".html")
            html = cache.read_text() if cache.exists() else loader.fetch(url)
            soup = BeautifulSoup(html, "html.parser")
            if not soup.find("h1") or not soup.select_one(".node-header, .exercise-profile"):
                raise CrawlStopped("Exercise page layout not recognized: " + url)
            atomic_text(cache, html)
            state["pages"][url] = {"videoUrl": extract_video(soup, url), "fetched_at": now()}
            processed += 1
            save()
            if processed == 1 or processed % 25 == 0:
                print(f"{len(state['pages'])}/{len(urls)} pages; {state['summary']['videos']} videos", flush=True)
    except (CrawlStopped, KeyboardInterrupt) as exc:
        error = str(exc) or "Interrupted"
        print("Stopped: " + error, flush=True)
    finally:
        save()
        if loader.owns_client:
            loader.client.close()
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "app/catalogs/sport_exercises.json")
    parser.add_argument("--output", type=Path, default=ROOT / "data/exercise_catalog/muscle_strength/videos")
    parser.add_argument("--delay", type=float, default=1.5)
    parser.add_argument("--max-pages", type=int)
    args = parser.parse_args()
    state = collect(json.loads(args.catalog.read_text()), args.output, delay=args.delay, max_pages=args.max_pages)
    print(json.dumps(state["summary"], ensure_ascii=False), flush=True)
    return 0 if state["summary"]["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
