"""Exercise listing crawl: pagination, deduplication, resumability and honest partial exports."""
import json

import httpx

from scripts.collect_muscle_strength import Collector, SOURCE, canonical, discover_categories, parse_listing

HOME = '''<main><h2>Exercises by Muscle Group</h2><div class="exercise-category-list">
<a href="/exercises/legs"><img alt="Legs"></a><a href="/exercises/legs">Legs</a>
<a href="/exercises/arms">Arms</a><a href="https://evil.test/exercises/legs">Ignore me</a>
</div><h2>Exercises by Equipment</h2><div class="exercise-category-list"><a href="/exercises/dumbbell">Dumbbell</a></div>
<div class="node-title"><a href="/exercises/featured.html">Not a category</a></div></main>'''


def listing(name, slug, *, pager='', equipment='Dumbbell'):
    return f'''<main><div class="cell"><div class="node-image"><img data-src="https://example.test/{slug}.jpg"></div>
    <div class="exercise-meta"><div class="meta-box"><label>Equipment</label>{equipment}</div>
    <div class="meta-box"><label>Type</label>Strength</div></div>
    <div class="node-title"><a href="/exercises/{slug}">{name}</a></div></div>{pager}
    <nav><a href="/exercises/menu-only">Not a card</a></nav></main>'''


def test_listing_parser_uses_actual_cards_and_safe_pagination():
    categories = discover_categories(HOME)
    assert len(categories) == 3
    assert categories[0] == {'url': SOURCE + '/legs', 'name_en': 'Legs', 'kind': 'muscle'}
    assert categories[-1]['kind'] == 'equipment'
    pager = '''<ul class="pager"><li><a href="?page=1">Next</a></li><li><a href="?page=0">First</a></li>
    <li><a href="?page=1&sort=latest">Filtered</a></li><li><a href="/exercises/arms?page=1">Different category</a></li>
    <li><a href="https://evil.test/exercises/legs?page=2">Other site</a></li></ul>'''
    rows, pages = parse_listing(listing('Squat', 'squat.html', pager=pager), SOURCE + '/legs')
    assert len(rows) == 1 and rows[0]['equipment'] == 'Dumbbell'
    assert rows[0]['preview_url'] == 'https://example.test/squat.html.jpg'
    assert pages == [SOURCE + '/legs', SOURCE + '/legs?page=1']
    assert canonical('http://muscleandstrength.com/exercises/squat.html/?utm_source=test#comments') == SOURCE + '/squat.html'


def test_crawl_resumes_and_exports_each_exercise_once(tmp_path, monkeypatch):
    monkeypatch.setattr('scripts.collect_muscle_strength.time.sleep', lambda _: None)
    hits = []
    pages = {
        '/robots.txt': 'User-agent: *\nDisallow: /admin/', '/exercises': HOME,
        '/exercises/legs': listing('Squat', 'squat.html', pager='<ul class="pager"><a href="?page=1">Next</a></ul>'),
        '/exercises/legs?page=1': listing('Lunge', 'lunge'),
        '/exercises/arms': listing('Curl', 'curl'),
        '/exercises/dumbbell': listing('Squat', 'squat.html'),
    }
    def reply(request):
        path = request.url.raw_path.decode()
        hits.append(path)
        return httpx.Response(200, text=pages[path])
    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        collector = Collector(tmp_path, max_pages=1, client=client)
        assert collector.run() == 2
        assert collector.export()['complete'] is False
        assert (tmp_path / 'exercise_urls.txt').read_text().splitlines() == [SOURCE + '/squat.html']
        collector = Collector(tmp_path, client=client)
        assert collector.run() == 0
        result = json.loads((tmp_path / 'exercises.json').read_text())
        assert result['summary']['complete'] is True
        assert result['summary']['listing_pages'] == 4
        assert result['summary']['unique_exercises'] == 3
        squat = next(e for e in result['exercises'] if e['name_en'] == 'Squat')
        assert squat['categories'] == ['Dumbbell', 'Legs']
        assert squat['muscle_groups'] == ['Legs'] and squat['equipment'] == ['Dumbbell']
        assert all(e['name_ru'] == '' for e in result['exercises'])
        assert len((tmp_path / 'exercise_urls.txt').read_text().splitlines()) == 3
        assert hits.count('/exercises/legs') == 1  # Resume never downloads a saved page again.
        assert (tmp_path / 'exercises.csv').read_bytes().startswith(b'\xef\xbb\xbf')
        initial_hits = len(hits)
        assert Collector(tmp_path, client=client).run() == 0
        assert hits[initial_hits:] == ['/robots.txt']
        (tmp_path / 'translations.ru.json').write_text(json.dumps({SOURCE + '/squat.html':'Присед', SOURCE + '/curl':'=1+1'}))
        assert Collector(tmp_path, client=client).run() == 0
        translated = json.loads((tmp_path / 'exercises.json').read_text())['exercises']
        assert next(e for e in translated if e['name_en']=='Squat')['name_ru']=='Присед'
        assert "'=1+1" in (tmp_path / 'exercises.csv').read_text()


def test_crawl_stops_on_block_and_keeps_recoverable_partial_result(tmp_path, monkeypatch):
    monkeypatch.setattr('scripts.collect_muscle_strength.time.sleep', lambda _: None)
    hits = []
    def reply(request):
        path = request.url.path
        hits.append(path)
        if path == '/robots.txt':
            return httpx.Response(200, text='User-agent: *\nDisallow: /admin/')
        if path == '/exercises':
            return httpx.Response(200, text=HOME)
        return httpx.Response(403, text='Blocked')
    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        assert Collector(tmp_path, client=client).run() == 2
    result = json.loads((tmp_path / 'summary.json').read_text())
    assert not result['complete'] and result['errors']
    assert result['unique_exercises'] == 0
    assert hits == ['/robots.txt', '/exercises', '/exercises/legs']
    assert (tmp_path / 'checkpoint.json').exists()


def test_crawl_respects_robots_and_does_not_fetch_forbidden_page(tmp_path, monkeypatch):
    monkeypatch.setattr('scripts.collect_muscle_strength.time.sleep', lambda _: None)
    hits = []
    def reply(request):
        hits.append(request.url.path)
        if request.url.path == '/robots.txt':
            return httpx.Response(200, text='User-agent: *\nDisallow: /exercises/legs')
        return httpx.Response(200, text=HOME)
    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        assert Collector(tmp_path, client=client).run() == 2
    assert hits == ['/robots.txt', '/exercises']
    assert 'robots.txt' in json.loads((tmp_path / 'summary.json').read_text())['errors'][SOURCE + '/legs']['message']
