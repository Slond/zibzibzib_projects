"""Video extraction, safe player URLs, and resumable source-page collection."""
import json

from bs4 import BeautifulSoup
import httpx
import pytest

from app.services.sport_media import extract_video, video_url
from scripts.collect_exercise_videos import collect

SOURCE = "https://www.muscleandstrength.com/exercises/"


@pytest.mark.parametrize(("value", "expected"), [
    ("//www.youtube.com/embed/uqI3GVwfToU?rel=0", "https://www.youtube.com/embed/uqI3GVwfToU"),
    ("https://youtu.be/uqI3GVwfToU", "https://www.youtube.com/embed/uqI3GVwfToU"),
    ("https://www.youtube.com/watch?v=uqI3GVwfToU&t=3", "https://www.youtube.com/embed/uqI3GVwfToU"),
    ("https://player.vimeo.com/video/877881471?badge=0&h=abc123", "https://player.vimeo.com/video/877881471?h=abc123"),
    ("https://vimeo.com/877881471/abc123", "https://player.vimeo.com/video/877881471?h=abc123"),
    ("https://youtube.com.evil.test/embed/uqI3GVwfToU", ""),
    ("https://user:pass@www.youtube.com/embed/uqI3GVwfToU", ""),
    ("https://www.youtube.com:444/embed/uqI3GVwfToU", ""),
    ("https://www.youtube.com/embed/invalid", ""),
    ("javascript:alert(1)", ""), (None, ""),
])
def test_player_normalization_rejects_arbitrary_embeds(value, expected):
    assert video_url(value) == expected


def test_article_player_wins_over_related_video_and_supports_lazy_loading():
    soup = BeautifulSoup('''<main><aside class="related"><iframe src="https://youtu.be/AAAAAAAAAAA"></iframe></aside>
    <div class="node-header"><iframe src="about:blank" data-src="//player.vimeo.com/video/877881471?h=abc123"></iframe></div></main>''', "html.parser")
    assert extract_video(soup, SOURCE + "leg-curl.html") == "https://player.vimeo.com/video/877881471?h=abc123"


def test_structured_video_metadata_survives_import_and_ignores_other_schema_urls():
    from app.services.sport_lookup import parse_exercise

    html = '''<h1>Front Squat Video Exercise Guide</h1>
    <div>Target Muscle Group Quads Exercise Type Strength Equipment Required Barbell Mechanics Compound</div>
    <script type="application/ld+json">invalid</script>
    <script type="application/ld+json">{"@graph":[{"@type":"Organization","url":"https://youtu.be/AAAAAAAAAAA"},
    {"@type":"VideoObject","embedUrl":"https://www.youtube.com/embed/9xAkoz95IFE?rel=0"}]}</script>'''
    result = parse_exercise(html, "Фронтальный присед", SOURCE + "front-squat.html")
    assert result["videoUrl"] == "https://www.youtube.com/embed/9xAkoz95IFE"
    assert result["en"] == "Front Squat"


def test_video_crawl_resumes_without_reloading_saved_pages_and_reports_absent_video(tmp_path, monkeypatch):
    monkeypatch.setattr("scripts.collect_muscle_strength.time.sleep", lambda _: None)
    catalog = {"exercises": {key: {"sourceUrl": SOURCE + key} for key in ["first", "second"]}}
    hits = []

    def reply(request):
        hits.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /admin/")
        frame = '<iframe src="https://youtu.be/uqI3GVwfToU"></iframe>' if request.url.path.endswith("first") else ""
        return httpx.Response(200, text='<main><h1>Exercise</h1><div class="node-header">' + frame + '</div></main>')

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        partial = collect(catalog, tmp_path, client=client, max_pages=1)
        assert not partial["summary"]["complete"]
        assert partial["summary"]["videos"] == 1
        result = collect(catalog, tmp_path, client=client)
        assert result["summary"]["complete"]
        assert result["summary"]["processed"] == 2
        assert result["summary"]["without_video"] == [SOURCE + "second"]
        assert hits.count("/exercises/first") == 1
        # A lost checkpoint is recoverable from cached HTML, without source-page requests.
        (tmp_path / "videos.json").unlink()
        hits.clear()
        assert collect(catalog, tmp_path, client=client)["summary"]["complete"]
        assert hits == ["/robots.txt"]


@pytest.mark.parametrize("block", [403, 429, "robots"])
def test_video_crawl_stops_on_access_restrictions(tmp_path, monkeypatch, block):
    monkeypatch.setattr("scripts.collect_muscle_strength.time.sleep", lambda _: None)
    hits = []

    def reply(request):
        hits.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: " + ("/exercises/" if block == "robots" else "/admin/"))
        return httpx.Response(block, text="Blocked")

    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        result = collect({"exercises": {"test": {"sourceUrl": SOURCE + "test"}}}, tmp_path, client=client)
    assert not result["summary"]["complete"] and result["summary"]["error"]
    assert result["summary"]["pending"] == [SOURCE + "test"]
    assert len(hits) == (1 if block == "robots" else 2)
    assert json.loads((tmp_path / "videos.json").read_text())["pages"] == {}


def test_instruction_parser_reads_wrapped_steps_without_tracking_items_or_next_section():
    from app.services.sport_lookup import parse_exercise
    html = '''<h1>Seated Dumbbell Front Raise Video Exercise Guide</h1>
    <div>Target Muscle Group Shoulders Exercise Type Strength Equipment Required Dumbbell Mechanics Isolation</div>
    <h2>Seated Dumbbell Front Raise Instructions</h2><span><div><ol>
    <li style="display:none">&nbsp;</li><li>Sit&nbsp;on a <a href="/">bench</a>.</li>
    <li>Raise your arms.<p>Keep a slight bend.</p></li><li>&nbsp;</li></ol></div></span>
    <h2>Tips</h2><ul><li>This belongs to another section.</li></ul>
    <footer><li>Subscribe</li></footer>'''
    result = parse_exercise(html, 'Подъём гантелей перед собой сидя', SOURCE + 'seated-dumbbell-front-raise.html')
    assert result['instructions'] == 'Sit on a bench.\nRaise your arms. Keep a slight bend.'
    assert result['muscle'] == 'Плечи'


def test_instruction_parser_supports_paragraphs_and_does_not_silently_truncate():
    from app.services.sport_lookup import parse_exercise
    html = '''<h1>Exercise Guide</h1><div>Target Muscle Group Upper Back Exercise Type Strength
    Equipment Required Kettle Bells Mechanics Compound</div><h3>Exercise Instructions</h3>
    <div><p>First step.</p><p>Second step.</p></div><h3>Comments</h3><p>Ignore.</p>'''
    result = parse_exercise(html, 'Упражнение', SOURCE + 'test.html')
    assert result['instructions'] == 'First step.\nSecond step.'
    assert result['muscle'] == 'Верх спины' and result['equipment'] == 'Гири'
    with pytest.raises(ValueError):
        parse_exercise(html.replace('First step.', 'x' * 4001), 'Упражнение', SOURCE + 'test.html')
