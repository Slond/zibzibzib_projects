#!/usr/bin/env python3
"""Extract complete source instructions from the video collector's saved HTML.

Run collect_exercise_videos.py first to fill missing pages. This step makes no
network requests and never publishes raw HTML. Rebuild with build_sport_catalog.py.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.sport_lookup import parse_exercise  # noqa: E402


def extract(catalog, cache):
    pages = {}
    for record in catalog['exercises'].values():
        url = record['sourceUrl']
        path = cache / (hashlib.sha256(url.encode()).hexdigest() + '.html')
        parsed = parse_exercise(path.read_text(), record['name'], url)
        if not parsed['instructions'].strip():
            raise ValueError('Missing source instructions: ' + url)
        pages[url] = {key: parsed[key] for key in ('instructions', 'muscle', 'equipment', 'bodyweight')}
    return {'version': 1, 'pages': pages,
            'summary': {'complete': True, 'total': len(pages),
                        'parsed_at': datetime.now(timezone.utc).isoformat()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', type=Path, default=ROOT / 'app/catalogs/sport_exercises.json')
    parser.add_argument('--cache', type=Path, default=ROOT / 'data/exercise_catalog/muscle_strength/videos/html')
    parser.add_argument('--output', type=Path, default=ROOT / 'data/exercise_catalog/muscle_strength/details.json')
    args = parser.parse_args()
    result = extract(json.loads(args.catalog.read_text()), args.cache)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(args.output)
    print('Extracted source instructions:', result['summary']['total'])


if __name__ == '__main__':
    main()
