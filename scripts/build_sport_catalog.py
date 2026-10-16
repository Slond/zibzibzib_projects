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


def build(source, translations):
    if not source['summary']['complete'] or source['summary']['errors']:
        raise ValueError('Only a completed, error-free collection can be published')
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
        result[key] = {
            'name':name, 'en':row['name_en'],
            'muscle':', '.join(MUSCLES[v] for v in row['muscle_groups']) or 'Не указано',
            'equipment':', '.join(EQUIPMENT[v] for v in row['equipment']) or 'Не указано',
            'instructions':'', 'sourceUrl':url, 'videoUrl':'', 'imageUrl':row['preview_url'],
            'attribution':'Muscle & Strength', 'bodyweight':row['equipment'] == ['Bodyweight'],
        }
    return {'version':1, 'source':source['summary']['source'], 'collected_at':source['summary']['updated_at'],
            'exercises':result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'data/exercise_catalog/muscle_strength/exercises.json')
    parser.add_argument('--translations', type=Path, default=ROOT / 'data/exercise_catalog/muscle_strength/translations.ru.json')
    parser.add_argument('--output', type=Path, default=ROOT / 'app/catalogs/sport_exercises.json')
    args = parser.parse_args()
    output = build(json.loads(args.source.read_text()), json.loads(args.translations.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(args.output)
    print('Shared catalog built:', len(output['exercises']), 'translated exercises')


if __name__ == '__main__':
    main()
