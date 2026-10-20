"""Shared catalog integrity, translation coverage and persisted exercise references."""
import copy
import json
from pathlib import Path
import re

import pytest

from app.services.sport import ExerciseDefinition, SportState, empty_workspace, upgrade_workspace
from app.services.sport_catalog import SHARED_EXERCISES
from app.services.sport_media import video_url
from scripts.build_sport_catalog import LEGACY, build


def test_shared_catalog_is_translated_unique_and_preserves_legacy_ids():
    assert len(SHARED_EXERCISES) == 1133
    urls = set()
    for key, record in SHARED_EXERCISES.items():
        ExerciseDefinition.model_validate(record)
        assert re.search('[А-Яа-яЁё]', record['name'])
        assert record['instructions'].strip()
        assert all(re.search('[А-Яа-яЁё]', step) for step in record['instructions'].splitlines())
        assert record['muscle'] != 'Не указано'
        assert all(step.strip() for step in record['instructions'].splitlines())
        assert record['en'] and record['sourceUrl'].startswith('https://www.muscleandstrength.com/exercises/')
        assert record['videoUrl'] and video_url(record['videoUrl']) == record['videoUrl']
        assert record['sourceUrl'] not in urls
        urls.add(record['sourceUrl'])
        assert key == LEGACY.get(record['sourceUrl'].rsplit('/',1)[-1], key)
    assert set(LEGACY.values()) <= SHARED_EXERCISES.keys()
    lower = empty_workspace()['templates']['lower']
    assert [e['id'] for e in lower['exercises']] == ['front','press','rdl','curl','calf']
    # Runtime references only bundled data; the ignored crawl directory is not required.
    artifact = Path(__file__).resolve().parents[1] / 'app/catalogs/sport_exercises.json'
    assert len(json.loads(artifact.read_text())['exercises']) == len(SHARED_EXERCISES)


def test_shared_reference_saves_without_copying_catalog_into_personal_workspace():
    shared_id = next(key for key, row in SHARED_EXERCISES.items() if 'One Leg Dumbbell Squat' in row['en'])
    doc = empty_workspace()
    doc['templates']['my_split'] = {'title':'Своя тренировка','place':'Дома','minutes':30,
                                   'exercises':[{'id':shared_id,'sets':2,'reps':'8–12','rest':90}]}
    doc['sessions']['2026-10-01'] = {'template':'my_split','originalTemplate':'my_split','status':'planned',
                                    'sets':{},'plan':copy.deepcopy(doc['templates']['my_split'])}
    result = SportState.model_validate(upgrade_workspace(doc))
    assert result.exercises == {}  # 1133 shared entries consume no personal exercise quota.
    assert result.sessions['2026-10-01'].plan.exercises[0].id == shared_id
    bad = copy.deepcopy(doc)
    bad['exercises'][shared_id] = copy.deepcopy(SHARED_EXERCISES[shared_id])
    with pytest.raises(ValueError, match='overwritten'):
        SportState.model_validate(bad)
    bad = copy.deepcopy(doc)
    bad['templates']['my_split']['exercises'][0]['id'] = 'ms_nonexistent'
    with pytest.raises(ValueError, match='unknown exercise'):
        SportState.model_validate(bad)


def test_builder_requires_complete_collection_and_all_translations():
    source = {'summary':{'complete':True,'errors':{},'source':'https://www.muscleandstrength.com/exercises','updated_at':'2026-10-17'},
              'exercises':[{'id':'ms_test','name_en':'Front Squat','url':'https://www.muscleandstrength.com/exercises/front-squat.html',
                            'muscle_groups':['Quads'],'equipment':['Barbell'],'preview_url':''}]}
    result = build(source, {'ms_test':'Фронтальный присед'})
    assert result['exercises']['front']['equipment'] == 'Штанга'
    assert result['exercises']['front']['muscle'] == 'Квадрицепсы'
    videos = {'version':1, 'summary':{'complete':True, 'error':'', 'updated_at':'2026-10-17'},
              'pages':{source['exercises'][0]['url']:{'videoUrl':'https://www.youtube.com/embed/9xAkoz95IFE'}}}
    assert build(source, {'ms_test':'Фронтальный присед'}, videos)['exercises']['front']['videoUrl'].endswith('9xAkoz95IFE')
    videos['summary']['complete'] = False
    with pytest.raises(ValueError, match='video collection'):
        build(source, {'ms_test':'Фронтальный присед'}, videos)
    with pytest.raises(ValueError, match='translation'):
        build(source, {})
    source['summary']['complete'] = False
    with pytest.raises(ValueError, match='completed'):
        build(source, {'ms_test':'Фронтальный присед'})


def test_shared_card_overrides_are_validated_and_do_not_change_the_catalog():
    doc = empty_workspace()
    original = copy.deepcopy(SHARED_EXERCISES['front'])
    doc['exerciseOverrides']['front'] = {'name': 'Мой присед', 'instructions': 'Свой шаг', 'bodyweight': False}
    result = SportState.model_validate(doc)
    assert result.exerciseOverrides['front']['name'] == 'Мой присед'
    assert 'muscle' not in result.exerciseOverrides['front']  # Still follows source metadata updates.
    assert result.exercises == {}
    assert SHARED_EXERCISES['front'] == original
    for patch in [{'name': ''}, {'bodyweight': 'yes'}, {'instructions': 'x' * 4001},
                  {'videoUrl': 'javascript:alert(1)'}, {'imageUrl': 'https://user:pass@example.test/x'},
                  {'unknown': 'x'}, {'muscle': None}]:
        bad = copy.deepcopy(doc)
        bad['exerciseOverrides']['front'] = patch
        with pytest.raises(ValueError):
            SportState.model_validate(bad)
    doc['exerciseOverrides']['nonexistent'] = {'name': 'Неизвестное'}
    with pytest.raises(ValueError, match='shared exercise'):
        SportState.model_validate(doc)


def test_builder_preserves_source_instructions_and_requires_full_details():
    source = {'summary': {'complete': True, 'errors': {}, 'source': 'source', 'updated_at': 'today'},
              'exercises': [{'id': 'ms_test', 'name_en': 'Front Squat',
                             'url': 'https://www.muscleandstrength.com/exercises/front-squat.html',
                             'muscle_groups': [], 'equipment': [], 'preview_url': ''}]}
    url = source['exercises'][0]['url']
    details = {'version': 1, 'summary': {'complete': True, 'parsed_at': 'today'},
               'pages': {url: {'instructions': 'First step.\nSecond step.', 'muscle': 'Квадрицепсы',
                               'equipment': 'Штанга', 'bodyweight': False}}}
    row = build(source, {'ms_test': 'Присед'}, details=details)['exercises']['front']
    assert row['instructions'] == details['pages'][url]['instructions']
    assert row['muscle'] == 'Квадрицепсы' and row['equipment'] == 'Штанга'
    details['pages'][url]['instructions'] = ''
    with pytest.raises(ValueError, match='empty'):
        build(source, {'ms_test': 'Присед'}, details=details)
    details['pages'] = {}
    with pytest.raises(ValueError, match='cover exactly'):
        build(source, {'ms_test': 'Присед'}, details=details)


def test_russian_instruction_publication_checks_alignment_numbers_and_source_version():
    import hashlib
    source = {'summary': {'complete': True, 'errors': {}, 'source': 'source', 'updated_at': 'today'},
              'exercises': [{'id': 'ms_test', 'name_en': 'Front Squat',
                             'url': 'https://www.muscleandstrength.com/exercises/front-squat.html',
                             'muscle_groups': ['Quads'], 'equipment': ['Barbell'], 'preview_url': ''}]}
    url = source['exercises'][0]['url']
    original = 'Bend to 90 degrees.\nRepeat 10 times.'
    details = {'version': 1, 'summary': {'complete': True, 'parsed_at': 'today'},
               'pages': {url: {'instructions': original}}}
    russian = {'version': 1, 'language': 'ru', 'translated_at': 'today', 'exercises': {
        url: {'source_sha256': hashlib.sha256(original.encode()).hexdigest(),
              'instructions': 'Согнитесь до угла 90 градусов.\nПовторите 10 раз.'}}}
    result = build(source, {'ms_test': 'Присед'}, details=details, instruction_translations=russian)
    assert result['instructions_language'] == 'ru'
    assert result['exercises']['front']['instructions'].startswith('Согнитесь')
    for bad_text, error in [('Один шаг.', 'preserve every'), ('Bend 90 degrees.\nRepeat 10 times.', 'preserve every'),
                            ('Согнитесь до 45 градусов.\nПовторите 10 раз.', 'preserve source numbers')]:
        bad = copy.deepcopy(russian)
        bad['exercises'][url]['instructions'] = bad_text
        with pytest.raises(ValueError, match=error):
            build(source, {'ms_test': 'Присед'}, details=details, instruction_translations=bad)
    russian['exercises'][url]['source_sha256'] = 'stale'
    with pytest.raises(ValueError, match='stale'):
        build(source, {'ms_test': 'Присед'}, details=details, instruction_translations=russian)
    russian['exercises'] = {}
    with pytest.raises(ValueError, match='cover exactly'):
        build(source, {'ms_test': 'Присед'}, details=details, instruction_translations=russian)


def test_import_reuses_russian_instructions_only_for_matching_source():
    import hashlib
    from app.services import sport_catalog
    original = 'Keep your back straight.'
    url = 'https://www.muscleandstrength.com/exercises/front-squat.html'
    saved = sport_catalog._TRANSLATED_SOURCES
    try:
        sport_catalog._TRANSLATED_SOURCES = {url: ('Держите спину прямо.', hashlib.sha256(original.encode()).hexdigest())}
        assert sport_catalog.translated_instructions(url, original) == 'Держите спину прямо.'
        assert sport_catalog.translated_instructions(url, original + ' New step.') == original + ' New step.'
        assert sport_catalog.translated_instructions(url + '?other', original) == original
    finally:
        sport_catalog._TRANSLATED_SOURCES = saved
