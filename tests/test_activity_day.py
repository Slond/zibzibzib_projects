import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.services.activity_day import (
    GeoPoint,
    PlaceSignature,
    ScreenSample,
    day_path,
    is_old_codex,
    km_from_home,
    merge_day,
    present_day,
    read_day,
)

TZ = ZoneInfo("Asia/Almaty")
DAY = date(2026, 9, 27)
HOME = (43.232226, 76.955359)
CAFE = (43.232515, 76.953059)
POINT_1330 = (43.23250, 76.95632)
POINT_1430 = (43.23235, 76.95318)
POINT_1930 = (43.23283, 76.95706)
POINT_2215 = (43.23254, 76.95624)


def at(hour, minute):
    return datetime(2026, 9, 27, hour, minute, tzinfo=TZ)


def point(hour, minute, lat, lon):
    return GeoPoint(at(hour, minute), lat, lon)


def screen(hour, minute, device, app, title, ssid=None):
    return ScreenSample(at(hour, minute), device, app, title, ssid)


def covers(rows, hour, minute):
    moment = at(hour, minute)
    found = []
    for row in rows:
        start = datetime.fromisoformat(row["start"])
        end = datetime.fromisoformat(row["end"])
        if start <= moment < end:
            found.append(row)
    return found


def september_27():
    points = []
    hour, minute = 3, 1
    while (hour, minute) <= (13, 0):
        points.append(point(hour, minute, *HOME))
        minute += 30
        hour += minute // 60
        minute %= 60
    points.append(point(13, 30, *POINT_1330))
    points.append(point(14, 0, *CAFE))
    points.append(point(14, 30, *POINT_1430))
    for hour, minute in (
        (15, 0),
        (15, 30),
        (16, 0),
        (16, 30),
        (17, 0),
        (17, 30),
        (18, 0),
        (18, 15),
        (18, 30),
        (18, 45),
        (19, 0),
        (19, 15),
    ):
        points.append(point(hour, minute, *HOME))
    points.append(point(19, 30, *POINT_1930))
    hour, minute = 19, 45
    while (hour, minute) <= (23, 45):
        lat, lon = POINT_2215 if (hour, minute) == (22, 15) else HOME
        points.append(point(hour, minute, lat, lon))
        minute += 15
        hour += minute // 60
        minute %= 60
    origin = datetime(2026, 9, 27, tzinfo=TZ)
    screens = []
    for minute in range(17 * 60 + 21, 19 * 60 + 15):
        screens.append(ScreenSample(origin + timedelta(minutes=minute), "Macbook", "Cursor", "activity.py"))
    for minute in range(15 * 60 + 48, 16 * 60 + 22):
        screens.append(ScreenSample(origin + timedelta(minutes=minute), "PC", "dota2", "Dota 2"))
    for minute in range(16 * 60 + 17, 16 * 60 + 23):
        screens.append(ScreenSample(origin + timedelta(minutes=minute), "Macbook", "Cursor", "activity.py"))
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    signatures = [
        PlaceSignature("Дом", *HOME, "Veter_2", created),
        PlaceSignature("Cafe Main", *CAFE, None, created),
    ]
    return points, screens, signatures


class MergeDayTest(unittest.TestCase):
    def test_labeled_27_september(self):
        points, screens, signatures = september_27()
        day = merge_day(DAY, TZ, points, screens, signatures, sample_minutes=1)

        cafe = covers(day["body"], 14, 15)[0]
        self.assertEqual(cafe["kind"], "stay")
        self.assertEqual(cafe["name"], "Cafe Main")
        self.assertEqual(covers(day["screen"], 14, 15), [])

        road = covers(day["body"], 13, 30)[0]
        self.assertEqual(road["kind"], "travel")

        home = covers(day["body"], 17, 30)[0]
        self.assertEqual(home["kind"], "stay")
        self.assertEqual(home["name"], "Дом")
        person = covers(day["screen"], 17, 30)[0]
        self.assertEqual(person["device"], "Macbook")
        self.assertEqual(covers(day["agent"], 17, 30), [])

        dispute = covers(day["disputes"], 16, 18)[0]
        self.assertEqual(dispute["winner"], "PC")
        self.assertEqual(covers(day["screen"], 16, 18)[0]["device"], "PC")
        self.assertEqual(covers(day["agent"], 16, 18)[0]["device"], "Macbook")

        self.assertEqual(covers(day["body"], 22, 15)[0]["kind"], "stay")
        self.assertEqual(covers(day["body"], 19, 30)[0]["kind"], "travel")

    def test_half_up_kilometers(self):
        self.assertEqual(km_from_home(1250), "1.3 км от дома")
        self.assertEqual(km_from_home(1249), "1.2 км от дома")

    def test_nearby_place_beats_the_network_name(self):
        created = datetime(2026, 1, 1, tzinfo=timezone.utc)
        points = [point(12, 0, *HOME), point(12, 30, *HOME)]
        screens = [
            screen(12, 10, "Macbook", "Cursor", "notes", "OtherNet"),
            screen(12, 11, "Macbook", "Cursor", "notes", "OtherNet"),
        ]
        day = merge_day(
            DAY,
            TZ,
            points,
            screens,
            [PlaceSignature("Дом", *HOME, "Veter_2", created)],
        )
        stay = covers(day["body"], 12, 10)[0]
        self.assertEqual(stay["name"], "Дом")
        self.assertNotIn("OtherNet", stay["name"])

    def test_one_app_on_one_computer_is_one_block(self):
        points = [point(1, 0, *HOME), point(2, 0, *HOME)]
        origin = datetime(2026, 9, 27, tzinfo=TZ)
        screens = [
            ScreenSample(origin + timedelta(minutes=60 + minute), "Macbook", "ChatGPT", f"chat {minute}")
            for minute in range(30)
        ]
        day = merge_day(DAY, TZ, points, screens, [])
        self.assertEqual(len(day["screen"]), 1)
        self.assertEqual(day["screen"][0]["app"], "ChatGPT")
        self.assertEqual(day["screen"][0]["title"], "ChatGPT")
        self.assertEqual(day["agent"], [])
        self.assertEqual(day["disputes"], [])

    def test_computer_use_on_the_same_computer_is_one_agent_block(self):
        points = [point(16, 0, *HOME), point(16, 40, *HOME)]
        origin = datetime(2026, 9, 27, tzinfo=TZ)
        screens = []
        for minute in range(16 * 60, 16 * 60 + 20):
            screens.append(
                ScreenSample(origin + timedelta(minutes=minute), "Macbook", "ChatGPT", "Computer Use Controls")
            )
            screens.append(
                ScreenSample(origin + timedelta(minutes=minute, seconds=30), "Macbook", "Cursor", f"file {minute}")
            )
        day = merge_day(DAY, TZ, points, screens, [])
        self.assertEqual(len(day["screen"]), 1)
        self.assertEqual(day["screen"][0]["app"], "Cursor")
        self.assertEqual(len(day["agent"]), 1)
        self.assertEqual(day["agent"][0]["app"], "ChatGPT")
        self.assertEqual(day["disputes"], [])

    def test_two_computers_make_one_dispute(self):
        points = [point(16, 0, *HOME), point(16, 40, *HOME)]
        origin = datetime(2026, 9, 27, tzinfo=TZ)
        screens = []
        for minute in range(16 * 60 + 15, 16 * 60 + 25):
            screens.append(ScreenSample(origin + timedelta(minutes=minute), "PC", "dota2", "Dota 2"))
            screens.append(
                ScreenSample(origin + timedelta(minutes=minute, seconds=10), "Macbook", "Cursor", f"file {minute}")
            )
        day = merge_day(DAY, TZ, points, screens, [])
        self.assertEqual(len(day["disputes"]), 1)
        self.assertEqual(len(day["screen"]), 1)
        self.assertEqual(len(day["agent"]), 1)

    def test_short_stay_between_roads_becomes_road(self):
        near = (43.23300, 76.96000)
        points = [
            point(10, 0, *HOME),
            point(10, 20, *HOME),
            point(10, 30, *near),
            point(10, 40, *CAFE),
            point(10, 44, *CAFE),
            point(11, 0, *HOME),
        ]
        day = merge_day(DAY, TZ, points, [], [])
        self.assertEqual(covers(day["body"], 10, 42)[0]["kind"], "travel")

    def test_computer_use_loses_when_another_screen_is_fresh(self):
        points = [point(16, 0, *HOME), point(16, 30, *HOME)]
        screens = [
            screen(16, 0, "Macbook", "ChatGPT", "Computer Use Controls"),
            screen(16, 10, "Macbook", "ChatGPT", "Computer Use Controls"),
            screen(16, 10, "PC", "Cursor", "activity.py"),
        ]
        day = merge_day(DAY, TZ, points, screens, [])
        self.assertEqual(covers(day["screen"], 16, 10)[0]["device"], "PC")

    def test_old_codex_file_is_detected(self):
        self.assertTrue(is_old_codex({"categories": [], "blocks": []}))
        self.assertFalse(is_old_codex({"body": [], "agent": []}))
        self.assertFalse(is_old_codex({"source": "codex", "body": [], "categories": [], "blocks": []}))

    def test_codex_blocks_are_not_replaced(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = day_path(root, 1, DAY)
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "categories": [{"id": "work", "name": "Работа", "color": "#1a73e8"}],
                        "blocks": [
                            {
                                "category": "work",
                                "device": "Macbook",
                                "start": "16:00",
                                "end": "18:00",
                                "items": [{"name": "Cursor", "start": "16:00", "end": "18:00"}],
                            }
                        ],
                        "agent": [
                            {
                                "device": "Macbook",
                                "start": "16:10",
                                "end": "16:40",
                                "note": "ChatGPT вёл окно",
                            }
                        ],
                        "aggregated_at": "2026-09-27T12:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            points = [point(16, 0, *HOME), point(17, 0, *HOME)]
            screens = [screen(16, 30, "Macbook", "Safari", "mail")]
            result = read_day(
                root,
                1,
                DAY,
                date(2026, 9, 28),
                TZ,
                points,
                screens,
                [],
                raw_latest=datetime(2026, 9, 27, 11, tzinfo=timezone.utc),
            )
            self.assertEqual(covers(result["screen"], 16, 30)[0]["title"], "Cursor")
            self.assertEqual(covers(result["screen"], 16, 30)[0]["color"], "#1a73e8")
            self.assertEqual(covers(result["agent"], 16, 20)[0]["title"], "ChatGPT вёл окно")
            again = read_day(
                root,
                1,
                DAY,
                date(2026, 9, 28),
                TZ,
                [point(18, 0, *CAFE), point(18, 30, *CAFE)],
                screens,
                [],
                raw_latest=datetime(2026, 9, 28, 1, tzinfo=timezone.utc),
            )
            self.assertEqual(covers(again["agent"], 16, 20)[0]["title"], "ChatGPT вёл окно")
            self.assertEqual(covers(again["screen"], 16, 30)[0]["title"], "Cursor")

    def test_closed_day_is_not_rebuilt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            points = [point(12, 0, *HOME), point(12, 30, *HOME)]
            first = read_day(
                root,
                1,
                DAY,
                date(2026, 9, 28),
                TZ,
                points,
                [],
                [],
                raw_latest=datetime(2026, 9, 27, 12, tzinfo=timezone.utc),
            )
            self.assertEqual(first["body"][1]["kind"], "stay")
            second = read_day(
                root,
                1,
                DAY,
                date(2026, 9, 28),
                TZ,
                [point(18, 0, *CAFE), point(18, 30, *CAFE)],
                [],
                [],
                raw_latest=datetime(2026, 9, 28, 1, tzinfo=timezone.utc),
            )
            kept = covers(second["body"], 18, 10)[0]
            self.assertEqual(kept["kind"], "stay")
            self.assertEqual(kept["center"]["lat"], HOME[0])

    def test_closed_day_renames_without_moving(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            points = [
                point(14, 0, *CAFE),
                point(14, 30, *CAFE),
                point(16, 0, *HOME),
                point(16, 30, *HOME),
            ]
            read_day(
                root,
                1,
                DAY,
                date(2026, 9, 28),
                TZ,
                points,
                [],
                [],
                raw_latest=datetime(2026, 9, 27, 12, tzinfo=timezone.utc),
            )
            signature = PlaceSignature("Cafe Main", *CAFE, None, at(14, 0), at(14, 0))
            second = read_day(
                root,
                1,
                DAY,
                date(2026, 9, 28),
                TZ,
                [point(18, 0, *HOME), point(18, 30, *HOME)],
                [],
                [signature],
                raw_latest=datetime(2026, 9, 28, 1, tzinfo=timezone.utc),
            )
            renamed = covers(second["body"], 14, 10)[0]
            self.assertEqual(renamed["name"], "Cafe Main")
            self.assertEqual(renamed["center"]["lat"], CAFE[0])


class PresentDayTest(unittest.TestCase):
    def test_morning_sits_above_evening(self):
        document = {
            "body": [
                {
                    "start": "2026-09-27T09:00:00+05:00",
                    "end": "2026-09-27T10:00:00+05:00",
                    "kind": "stay",
                    "name": "зал",
                },
                {
                    "start": "2026-09-27T18:00:00+05:00",
                    "end": "2026-09-27T19:00:00+05:00",
                    "kind": "stay",
                    "name": "дом",
                },
            ],
            "screen": [
                {
                    "start": "2026-09-27T09:00:00+05:00",
                    "end": "2026-09-27T09:30:00+05:00",
                    "app": "Cursor",
                    "title": "day",
                    "device": "Macbook",
                }
            ],
            "agent": [],
            "disputes": [],
        }
        view = present_day(document, document["body"][0]["start"])
        self.assertLess(view["body_blocks"][0]["top"], view["body_blocks"][1]["top"])
        self.assertAlmostEqual(view["body_blocks"][0]["top"], 9 * 60 / 1440 * 100)
        self.assertTrue(view["body_blocks"][0]["selected"])
        self.assertEqual(view["screen_sum"], "30 мин")
        self.assertEqual(view["selected_name"], "зал")
        self.assertEqual(view["columns"][0]["title"], "тело")
        self.assertEqual(view["hours"][0]["label"], "00:00")
        self.assertEqual(view["hours"][1]["label"], "00:15")
        self.assertTrue(view["hours"][4]["major"])
        self.assertFalse(view["hours"][1]["major"])
        self.assertEqual(view["hours"][-1]["label"], "24:00")

    def test_template_draws_vertical_day(self):
        from types import SimpleNamespace

        try:
            from jinja2 import Environment, FileSystemLoader, select_autoescape
        except ModuleNotFoundError:
            self.skipTest("jinja2 is not installed in this interpreter")

        env = Environment(
            loader=FileSystemLoader("app/templates"),
            autoescape=select_autoescape(["html"]),
        )
        document = {
            "error": "raw",
            "body": [],
            "screen": [],
            "agent": [],
            "disputes": [],
        }
        failed = present_day(document, None)
        failed.update(
            day="2026-09-27",
            day_label="воскресенье 27 сентября",
            is_today=False,
            prev_date="2026-09-26",
            next_date="2026-09-28",
            has_devices=True,
            has_places=True,
            user=SimpleNamespace(name="Я", email="a@b.c"),
            page="day",
            error=None,
            success=None,
        )
        html = env.get_template("activity/index.html").render(**failed)
        self.assertIn("День не собрался. Сырые события не прочитались.", html)
        self.assertNotIn("Запомнить", html)
        self.assertIn("тело", html)

        ok = present_day(
            {
                "body": [
                    {
                        "start": "2026-09-27T09:00:00+05:00",
                        "end": "2026-09-27T10:00:00+05:00",
                        "kind": "stay",
                        "name": "зал",
                    }
                ],
                "screen": [],
                "agent": [],
                "disputes": [
                    {
                        "start": "2026-09-27T16:18:00+05:00",
                        "end": "2026-09-27T16:19:00+05:00",
                        "screens": [
                            {"device": "Macbook", "app": "Cursor", "title": "a"},
                            {"device": "PC", "app": "Dota 2", "title": "b"},
                        ],
                        "winner": "PC",
                    }
                ],
            },
            "2026-09-27T09:00:00+05:00",
        )
        ok.update(
            day="2026-09-27",
            day_label="воскресенье 27 сентября",
            is_today=False,
            prev_date="2026-09-26",
            next_date="2026-09-28",
            has_devices=True,
            has_places=True,
            user=SimpleNamespace(name="Я", email="a@b.c"),
            page="day",
            error=None,
            success=None,
        )
        drawn = env.get_template("activity/index.html").render(**ok)
        self.assertIn("Запомнить", drawn)
        self.assertIn("ты за экраном", drawn)
        self.assertIn("Имя места", drawn)
        self.assertIn("data-start=\"2026-09-27T09:00:00+05:00\"", drawn)
        self.assertIn("Экран отдан PC", drawn)


if __name__ == "__main__":
    unittest.main()
