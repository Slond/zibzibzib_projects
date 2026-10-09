"""Check the DOM contract of the finance pages after rendering Jinja."""
import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape


class PageElements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = Counter()

    def handle_starttag(self, tag, attrs):
        element_id = dict(attrs).get('id')
        if element_id:
            self.ids[element_id] += 1


@pytest.mark.parametrize('page', ['index', 'analytics'])
@pytest.mark.parametrize('with_events', [False, True])
def test_finance_scripts_find_their_elements(page, with_events):
    templates = Path(__file__).resolve().parents[1] / 'app' / 'templates'
    env = Environment(loader=FileSystemLoader(templates), autoescape=select_autoescape())
    events = [SimpleNamespace(id=1, name='One'), SimpleNamespace(id=2, name='Two')] if with_events else []
    html = env.get_template(f'finance/{page}.html').render(
        request=SimpleNamespace(query_params={}), events=events,
        selected_event=events[0] if events else None, period='month', budget_alert=None,
    )
    elements = PageElements()
    elements.feed(html)
    script_ids = set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", html))
    assert not (script_ids - elements.ids.keys()), 'JavaScript references missing page elements'
    assert all(count == 1 for count in elements.ids.values()), 'Duplicate DOM IDs'
