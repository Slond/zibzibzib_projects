"""Shared catalog integrity, translation coverage and persisted exercise references."""
import copy
import json
from pathlib import Path
import re

import pytest

from app.services.sport import ExerciseDefinition, SportState, empty_workspace, upgrade_workspace
from app.services.sport_catalog import SHARED_EXERCISES
from scripts.build_sport_catalog import LEGACY, build


def test_shared_catalog_is_translated_unique_and_preserves_legacy_ids():
    assert len(SHARED_EXERCISES) == 1133
    urls = set()
    for key, record in SHARED_EXERCISES.items():
        ExerciseDefinition.model_validate(record)
        assert re.search('[А-Яа-яЁё]', record['name'])
        assert record['en'] and record['sourceUrl'].startswith('https://www.muscleandstrength.com/exercises/')
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
    with pytest.raises(ValueError, match='translation'):
        build(source, {})
    source['summary']['complete'] = False
    with pytest.raises(ValueError, match='completed'):
        build(source, {'ms_test':'Фронтальный присед'})
