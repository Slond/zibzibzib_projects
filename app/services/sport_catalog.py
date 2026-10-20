"""Bundled, read-only exercise library; independent of personal workspaces."""
import json
import hashlib
from pathlib import Path
import re

_DOCUMENT = json.loads((Path(__file__).resolve().parents[1] / 'catalogs/sport_exercises.json').read_text())
SHARED_EXERCISES = _DOCUMENT['exercises']
SHARED_EXERCISE_IDS = frozenset(SHARED_EXERCISES)
if _DOCUMENT['version'] != 1 or any(not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', key) for key in SHARED_EXERCISES):
    raise ValueError('Invalid bundled exercise catalog')

_TRANSLATED_SOURCES = {
    row['sourceUrl']: (row['instructions'], _DOCUMENT['instruction_source_hashes'][key])
    for key, row in SHARED_EXERCISES.items()
    if _DOCUMENT.get('instructions_language') == 'ru' and key in _DOCUMENT.get('instruction_source_hashes', {})
}


def translated_instructions(url, original):
    """Reuse a checked translation only while the source steps still match exactly."""
    translated = _TRANSLATED_SOURCES.get(url)
    if translated and hashlib.sha256(original.encode()).hexdigest() == translated[1]:
        return translated[0]
    return original
