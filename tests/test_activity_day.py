"""Behavioral checks for truthful activity time and location coverage."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from app.services.activity_day import (
    GeoPoint,
    PlaceSignature,
    ScreenSample,
    merge_day,
    present_day,
    km_from_home,
)
from app.services.activity_pipeline import bounds, signatures_from_places

TZ = ZoneInfo("Asia/Almaty")
DAY = date(2026, 9, 27)
HOME = (43.232226, 76.955359)


def at(h, m=0):
    return datetime(2026, 9, 27, h, m, tzinfo=TZ)


def sample(h, m=0, **kw):
    return ScreenSample(at(h, m), "Mac", "Editor", "document", device_id=1, **kw)


def active(rows, at):
    return [
        r
        for r in rows
        if datetime.fromisoformat(r["start"]) <= at < datetime.fromisoformat(r["end"])
    ]


def seconds(rows):
    return sum(
        (
            datetime.fromisoformat(r["end"]) - datetime.fromisoformat(r["start"])
        ).total_seconds()
        for r in rows
    )


def test_title_changes_keep_observed_minutes():
    events = [
        ScreenSample(
            at(10) + timedelta(minutes=i), "Mac", "Browser", f"tab {i}", device_id=1
        )
        for i in range(30)
    ]
    doc = merge_day(DAY, TZ, [], events)
    assert seconds(doc["screen"]) == 1800
    assert present_day(doc)["screen_sum"] == "30 мин"
    assert doc["screen"][0]["title"] == "tab 0"


def test_sparse_events_do_not_invent_gap_activity():
    doc = merge_day(DAY, TZ, [], [sample(10), sample(11)])
    assert seconds(doc["screen"]) == 120
    assert not active(doc["screen"], at(10, 30))


def test_stored_sampling_interval_survives_settings_changes():
    doc = merge_day(DAY, TZ, [], [sample(10, sample_minutes=5)], sample_minutes=1)
    assert seconds(doc["screen"]) == 300


def test_lone_computer_use_is_agent():
    doc = merge_day(
        DAY,
        TZ,
        [],
        [ScreenSample(at(10), "Mac", "ChatGPT", "Computer Use Controls", device_id=1)],
    )
    assert not doc["screen"] and seconds(doc["agent"]) == 60


def test_idle_is_preserved_but_not_asserted_as_human_or_agent():
    doc = merge_day(DAY, TZ, [], [sample(10, idle_seconds=600)], idle_minutes=5)
    assert not doc["screen"] and not doc["agent"]
    assert seconds(doc["uncertain"]) == 60
    assert len(present_day(doc)["columns"]) == 4


def test_identical_device_names_do_not_merge_devices():
    screens = [sample(10), ScreenSample(at(10), "Mac", "Game", "Game", device_id=2)]
    doc = merge_day(DAY, TZ, [], screens)
    assert seconds(doc["screen"]) == 60 and seconds(doc["uncertain"]) == 60
    assert {r["device_id"] for key in ("screen", "uncertain") for r in doc[key]} == {
        1,
        2,
    }
    assert not doc["agent"]


def test_contiguous_conflict_is_one_explanation():
    screens = []
    for i in range(30):
        screens += [
            ScreenSample(
                at(10) + timedelta(minutes=i), "PC", "Game", "Game", device_id=1
            ),
            ScreenSample(
                at(10) + timedelta(minutes=i, seconds=10),
                "Mac",
                "Editor",
                "Editor",
                device_id=2,
            ),
        ]
    doc = merge_day(DAY, TZ, [], screens)
    assert len(doc["disputes"]) == 1
    assert seconds(doc["screen"]) == 1810
    assert not doc["agent"]


def test_overlapping_agent_durations_use_union():
    rows = [
        dict(start=at(10).isoformat(), end=at(11).isoformat(), title="A"),
        dict(start=at(10, 30).isoformat(), end=at(11, 30).isoformat(), title="B"),
    ]
    assert present_day({"agent": rows})["agent_sum"] == "1 ч 30 мин"


def test_today_stops_at_now():
    doc = merge_day(
        DAY,
        TZ,
        [GeoPoint(at(10), *HOME), GeoPoint(at(10, 15), *HOME)],
        [sample(10, 15)],
        now=at(10, 15) + timedelta(seconds=10),
    )
    assert seconds(doc["screen"]) == 10
    assert all(
        datetime.fromisoformat(r["end"]) <= at(10, 15) + timedelta(seconds=10)
        for r in doc["body"]
    )


def test_location_stops_after_45_minutes_without_evidence():
    points = [GeoPoint(at(10), *HOME), GeoPoint(at(10, 15), *HOME)]
    doc = merge_day(DAY, TZ, points, [])
    assert active(doc["body"], at(10, 30))[0]["kind"] == "stay"
    assert active(doc["body"], at(11, 1))[0]["kind"] == "unknown"
    assert active(doc["body"], at(23))[0]["kind"] == "unknown"


def test_bad_accuracy_does_not_move_body():
    points = [
        GeoPoint(at(10), *HOME, 10),
        GeoPoint(at(10, 15), *HOME, 10),
        GeoPoint(at(10, 30), 40, 70, 500),
    ]
    doc = merge_day(DAY, TZ, points, [])
    assert doc["poor_location_points"] == 1
    assert active(doc["body"], at(10, 30))[0]["kind"] == "stay"


def test_radius_and_ssid_are_consistent():
    pins = [PlaceSignature("Home", *HOME, "HomeNet", at(0), radius_m=10)]
    points = [
        GeoPoint(at(10), HOME[0] + 0.0003, HOME[1]),
        GeoPoint(at(10, 15), HOME[0] + 0.0003, HOME[1]),
    ]
    doc = merge_day(DAY, TZ, points, [], pins)
    assert active(doc["body"], at(10, 10))[0]["name"] != "Home"
    pins = [PlaceSignature("Home", *HOME, "HomeNet", at(0), radius_m=80)]
    doc = merge_day(DAY, TZ, points, [sample(10, ssid="OtherNet")], pins)
    assert active(doc["body"], at(10, 10))[0]["name"] != "Home"


def test_place_names_keep_effective_history():
    places = [
        dict(
            name="New",
            latitude=HOME[0],
            longitude=HOME[1],
            wifi_ssid=None,
            radius_m=80,
            labels_json='[{"name":"Old","at":"2026-09-01T00:00:00Z"},{"name":"New","at":"2026-09-28T00:00:00Z"}]',
        )
    ]
    doc = merge_day(
        DAY,
        TZ,
        [GeoPoint(at(10), *HOME), GeoPoint(at(10, 15), *HOME)],
        [],
        signatures_from_places(places),
    )
    assert active(doc["body"], at(10, 10))[0]["name"] == "Old"


def test_timezone_day_bounds_use_actual_dst_duration():
    zone = ZoneInfo("Europe/Berlin")
    assert (
        bounds(date(2026, 3, 29), zone)[1] - bounds(date(2026, 3, 29), zone)[0]
    ).total_seconds() == 23 * 3600
    assert (
        bounds(date(2026, 10, 25), zone)[1] - bounds(date(2026, 10, 25), zone)[0]
    ).total_seconds() == 25 * 3600


def test_pre_midnight_interval_carries_over():
    before = at(0) - timedelta(seconds=30)
    doc = merge_day(
        DAY, TZ, [], [ScreenSample(before, "Mac", "Editor", "Doc", device_id=1)]
    )
    assert (
        seconds(doc["screen"]) == 30 and doc["screen"][0]["start"] == at(0).isoformat()
    )


def test_half_up_kilometers():
    assert km_from_home(1250) == "1.3 км от дома"
    assert km_from_home(1249) == "1.2 км от дома"


def test_short_stay_between_roads_becomes_road():
    other = (43.24, 76.97)
    third = (43.25, 76.98)
    points = [
        GeoPoint(at(10), *HOME),
        GeoPoint(at(10, 15), *HOME),
        GeoPoint(at(10, 25), *other),
        GeoPoint(at(10, 27), *other),
        GeoPoint(at(10, 35), *third),
        GeoPoint(at(10, 50), *third),
    ]
    doc = merge_day(DAY, TZ, points, [])
    assert active(doc["body"], at(10, 26))[0]["kind"] == "travel"
    assert active(doc["body"], at(10, 40))[0]["kind"] == "stay"


def test_long_location_gap_is_unknown_even_at_same_place():
    points = [
        GeoPoint(at(10), *HOME),
        GeoPoint(at(10, 15), *HOME),
        GeoPoint(at(12), *HOME),
        GeoPoint(at(12, 15), *HOME),
    ]
    assert (
        active(merge_day(DAY, TZ, points, [])["body"], at(11))[0]["kind"] == "unknown"
    )


def test_location_stay_carries_across_midnight():
    points = [
        GeoPoint(at(0) - timedelta(minutes=20), *HOME),
        GeoPoint(at(0) - timedelta(minutes=5), *HOME),
    ]
    doc = merge_day(DAY, TZ, points, [])
    assert active(doc["body"], at(0, 20))[0]["kind"] == "stay"
    assert active(doc["body"], at(0, 41))[0]["kind"] == "unknown"
    assert all(datetime.fromisoformat(r["start"]) >= at(0) for r in doc["body"])


def test_fall_back_retains_both_observations_at_repeated_clock_time():
    day = date(2026, 10, 25)
    zone = ZoneInfo("Europe/Berlin")
    first = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=0)
    second = datetime(2026, 10, 25, 2, 30, tzinfo=zone, fold=1)
    doc = merge_day(
        day,
        zone,
        [],
        [
            ScreenSample(first, "Mac", "Editor", "A", device_id=1),
            ScreenSample(second, "Mac", "Editor", "B", device_id=1),
        ],
    )
    doc.update(day=day.isoformat(), timezone=str(zone))
    view = present_day(doc)
    assert view["screen_sum"] == "2 мин"
    assert view["screen_blocks"][1]["top"] > view["screen_blocks"][0]["top"]


def test_renaming_mid_stay_does_not_relabel_prior_hours():
    pins = [
        PlaceSignature("Old", *HOME, None, at(0)),
        PlaceSignature("New", *HOME, None, at(10, 20)),
    ]
    doc = merge_day(
        DAY,
        TZ,
        [
            GeoPoint(at(10), *HOME),
            GeoPoint(at(10, 15), *HOME),
            GeoPoint(at(10, 30), *HOME),
        ],
        [],
        pins,
    )
    assert active(doc["body"], at(10, 10))[0]["name"] == "Old"
    assert active(doc["body"], at(10, 25))[0]["name"] == "New"
