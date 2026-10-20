#!/usr/bin/env python3
"""Build the shared, translated exercise catalog from a completed collection and reviewed names."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.sport_media import video_url  # noqa: E402
from app.services.sport_source import MUSCLES, EQUIPMENT  # noqa: E402

# Keep all identifiers already referenced by personal training histories.
LEGACY = {'wide-stance-45-degree-leg-press.html':'press', 'front-squat.html':'front',
          'romanian-deadlift':'rdl', 'leg-curl.html':'curl', 'standing-calf-raise.html':'calf',
          'bodyweight-squat.html':'squat', 'bodyweight-glute-bridge':'bridge',
          'dumbbell-bench-press.html':'bench', 'bent-over-dumbbell-row.html':'row'}


def build(source, translations, videos=None, details=None, instruction_translations=None):
    if not source['summary']['complete'] or source['summary']['errors']:
        raise ValueError('Only a completed, error-free collection can be published')
    if videos is not None:
        if videos.get('version') != 1 or not videos['summary']['complete'] or videos['summary']['error']:
            raise ValueError('Only a completed, error-free video collection can be published')
        if set(videos['pages']) != {row['url'] for row in source['exercises']}:
            raise ValueError('Video collection must cover exactly the source exercises')
    if details is not None:
        if details.get('version') != 1 or not details['summary']['complete']:
            raise ValueError('Only complete exercise details can be published')
        if set(details['pages']) != {row['url'] for row in source['exercises']}:
            raise ValueError('Exercise details must cover exactly the source exercises')
        if any(not p.get('instructions', '').strip() for p in details['pages'].values()):
            raise ValueError('Exercise instructions cannot be empty')
    if instruction_translations is not None:
        if details is None or instruction_translations.get('version') != 1 or instruction_translations.get('language') != 'ru':
            raise ValueError('Russian instruction translations require source details')
        if set(instruction_translations['exercises']) != set(details['pages']):
            raise ValueError('Instruction translations must cover exactly the source exercises')
    result = {}
    sources = set()
    for row in source['exercises']:
        url = row['url']
        name = translations.get(url, translations.get(row['id'], '')).strip()
        if not name or not re.search(r'[А-Яа-яЁё]', name):
            raise ValueError('Missing reviewed Russian translation: ' + row['name_en'])
        if url in sources:
            raise ValueError('Duplicate source URL')
        sources.add(url)
        key = LEGACY.get(url.rsplit('/', 1)[-1], row['id'])
        if key in result:
            raise ValueError('Duplicate exercise identifier')
        video = videos['pages'][url]['videoUrl'] if videos is not None else ''
        if video and video_url(video) != video:
            raise ValueError('Invalid or noncanonical video URL: ' + url)
        parsed = details['pages'][url] if details is not None else {}
        instructions = parsed.get('instructions', '')
        if instruction_translations is not None:
            translated = instruction_translations['exercises'][url]
            digest = hashlib.sha256(instructions.encode()).hexdigest()
            if translated['source_sha256'] != digest:
                raise ValueError('Instruction translation is stale: ' + url)
            russian = translated['instructions']
            source_steps, translated_steps = instructions.splitlines(), russian.splitlines()
            if len(source_steps) != len(translated_steps) or not all(re.search('[А-Яа-яЁё]', step) for step in translated_steps):
                raise ValueError('Instruction translation must preserve every source step: ' + url)
            numbers = lambda text: [v.replace(',', '.') for v in re.findall(r'\d+(?:[.,]\d+)?', text)]
            if any(numbers(original) != numbers(translation) for original, translation in zip(source_steps, translated_steps)):
                raise ValueError('Instruction translation must preserve source numbers: ' + url)
            if len(russian) > 4000:
                raise ValueError('Translated instructions exceed the card limit: ' + url)
            instructions = russian
        result[key] = {
            'name':name, 'en':row['name_en'],
            'muscle':', '.join(MUSCLES[v] for v in row['muscle_groups']) or 'Не указано',
            'equipment':', '.join(EQUIPMENT[v] for v in row['equipment']) or 'Не указано',
            'instructions':instructions, 'sourceUrl':url, 'videoUrl':video, 'imageUrl':row['preview_url'],
            'attribution':'Muscle & Strength', 'bodyweight':row['equipment'] == ['Bodyweight'],
        }
        for field in ('muscle', 'equipment', 'bodyweight'):
            if field in parsed:
                result[key][field] = parsed[field]
    document = {'version':1, 'source':source['summary']['source'], 'collected_at':source['summary']['updated_at'],
                'exercises':result}
    if videos is not None:
        document['videos_collected_at'] = videos['summary']['updated_at']
    if details is not None:
        document['instructions_parsed_at'] = details['summary']['parsed_at']
    if instruction_translations is not None:
        document['instructions_language'] = 'ru'
        document['instructions_translated_at'] = instruction_translations['translated_at']
        document['instruction_source_hashes'] = {
            key: instruction_translations['exercises'][row['sourceUrl']]['source_sha256']
            for key, row in result.items()
        }
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'data/exercise_catalog/muscle_strength/exercises.json')
    parser.add_argument('--translations', type=Path, default=ROOT / 'data/exercise_catalog/muscle_strength/translations.ru.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'app/catalogs/sport_exercises.json')
    parser.add_argument('--videos', type=Path, default=ROOT / 'data/exercise_catalog/muscle_strength/videos/videos.json')
    parser.add_argument('--details', type=Path, default=ROOT / 'data/exercise_catalog/muscle_strength/details.json')
    parser.add_argument('--instruction-translations', type=Path,
                        default=ROOT / 'data/exercise_catalog/muscle_strength/instructions.ru.json')
    args = parser.parse_args()
    videos = json.loads(args.videos.read_text()) if args.videos.exists() else None
    details = json.loads(args.details.read_text()) if args.details.exists() else None
    instruction_translations = json.loads(args.instruction_translations.read_text()) if args.instruction_translations.exists() else None
    output = build(json.loads(args.source.read_text()), json.loads(args.translations.read_text()), videos, details, instruction_translations)
    if instruction_translations is None and args.output.exists():
        previous = json.loads(args.output.read_text())
        if previous.get('instructions_language') == 'ru':
            raise ValueError('Refusing to replace Russian instructions: supply --instruction-translations')
    if details is None and args.output.exists():
        previous = json.loads(args.output.read_text())
        if any(e.get('instructions') for e in previous['exercises'].values()):
            raise ValueError('Refusing to erase published instructions: supply --details')
    if videos is None and args.output.exists():
        previous = json.loads(args.output.read_text())
        if any(e.get('videoUrl') for e in previous['exercises'].values()):
            raise ValueError('Refusing to erase published videos: supply the completed --videos collection')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(args.output)
    print('Shared catalog built:', len(output['exercises']), 'translated exercises')


if __name__ == '__main__':
    main()
