#!/usr/bin/env python3
"""Build the shared, translated exercise catalog from a completed collection and reviewed names."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.sport_media import video_url  # noqa: E402

# Keep all identifiers already referenced by personal training histories.
LEGACY = {'wide-stance-45-degree-leg-press.html':'press', 'front-squat.html':'front',
          'romanian-deadlift':'rdl', 'leg-curl.html':'curl', 'standing-calf-raise.html':'calf',
          'bodyweight-squat.html':'squat', 'bodyweight-glute-bridge':'bridge',
          'dumbbell-bench-press.html':'bench', 'bent-over-dumbbell-row.html':'row'}
MUSCLES = {'Quads':'Квадрицепсы', 'Hamstrings':'Задняя поверхность бедра', 'Glutes':'Ягодицы',
           'Chest':'Грудь', 'Lats':'Широчайшие', 'Upper Back':'Верх спины', 'Lower Back':'Поясница',
           'Shoulders':'Плечи', 'Biceps':'Бицепс', 'Triceps':'Трицепс', 'Abs':'Пресс', 'Calves':'Икры',
           'Forearms':'Предплечья', 'Traps':'Трапеции', 'Obliques':'Косые мышцы живота',
           'Hip Flexors':'Сгибатели бедра', 'Adductors':'Приводящие мышцы бедра',
           'Abductors':'Отводящие мышцы бедра', 'IT Band':'Подвздошно-большеберцовый тракт',
           'Neck':'Шея', 'Palmar Fascia':'Ладонная фасция', 'Plantar Fascia':'Подошвенная фасция'}
EQUIPMENT = {'Dumbbell':'Гантели', 'Bodyweight':'Без инвентаря', 'Barbell':'Штанга',
             'Cable':'Блочный тренажёр', 'Machine':'Тренажёр', 'Kettle Bells':'Гири',
             'Bands':'Резинка', 'Exercise Ball':'Фитбол', 'Other':'Другой инвентарь',
             'EZ Bar':'EZ-гриф', 'Foam Roll':'Массажный ролик', 'Medicine Ball':'Медицинбол',
             'Sled':'Сани', 'Box':'Тумба', 'Jump Rope':'Скакалка', 'Lacrosse Ball':'Мяч для лакросса',
             'Trap Bar':'Трэп-гриф', 'Tiger Tail':'Ролик Tiger Tail', 'Chains':'Цепи',
             'Landmine':'Лэндмайн', 'Bench':'Скамья', 'Rings':'Кольца', 'Valslide':'Скользящие диски Valslide',
             'Hip Thruster':'Тренажёр Hip Thruster', 'Fat Bar':'Толстый гриф', 'Rope':'Канат',
             'Safety Bar':'Гриф Safety Bar', 'Tire':'Покрышка'}


def build(source, translations, videos=None):
    if not source['summary']['complete'] or source['summary']['errors']:
        raise ValueError('Only a completed, error-free collection can be published')
    if videos is not None:
        if videos.get('version') != 1 or not videos['summary']['complete'] or videos['summary']['error']:
            raise ValueError('Only a completed, error-free video collection can be published')
        if set(videos['pages']) != {row['url'] for row in source['exercises']}:
            raise ValueError('Video collection must cover exactly the source exercises')
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
        result[key] = {
            'name':name, 'en':row['name_en'],
            'muscle':', '.join(MUSCLES[v] for v in row['muscle_groups']) or 'Не указано',
            'equipment':', '.join(EQUIPMENT[v] for v in row['equipment']) or 'Не указано',
            'instructions':'', 'sourceUrl':url, 'videoUrl':video, 'imageUrl':row['preview_url'],
            'attribution':'Muscle & Strength', 'bodyweight':row['equipment'] == ['Bodyweight'],
        }
    document = {'version':1, 'source':source['summary']['source'], 'collected_at':source['summary']['updated_at'],
                'exercises':result}
    if videos is not None:
        document['videos_collected_at'] = videos['summary']['updated_at']
    return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'data/exercise_catalog/muscle_strength/exercises.json')
    parser.add_argument('--translations', type=Path, default=ROOT / 'data/exercise_catalog/muscle_strength/translations.ru.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'app/catalogs/sport_exercises.json')
    parser.add_argument('--videos', type=Path, default=ROOT / 'data/exercise_catalog/muscle_strength/videos/videos.json')
    args = parser.parse_args()
    videos = json.loads(args.videos.read_text()) if args.videos.exists() else None
    output = build(json.loads(args.source.read_text()), json.loads(args.translations.read_text()), videos)
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
