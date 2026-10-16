"""Bundled, read-only exercise library; independent of personal workspaces."""
import json
from pathlib import Path
import re

_DOCUMENT = json.loads((Path(__file__).resolve().parents[1] / 'catalogs/sport_exercises.json').read_text())
SHARED_EXERCISES = _DOCUMENT['exercises']
SHARED_EXERCISE_IDS = frozenset(SHARED_EXERCISES)
if _DOCUMENT['version'] != 1 or any(not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', key) for key in SHARED_EXERCISES):
    raise ValueError('Invalid bundled exercise catalog')
