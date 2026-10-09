"""Exercise the actual persisted outbox across offline captures and lost acknowledgements."""

import importlib.util
import json
from pathlib import Path
import httpx

spec = importlib.util.spec_from_file_location(
    "collector", Path(__file__).parents[1] / "clients/activity/client.py"
)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


def test_offline_config_and_post_keep_capturing_across_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "front_window", lambda: ("Editor", "Doc", None))
    monkeypatch.setattr(collector, "wifi_ssid", lambda: None)
    monkeypatch.setattr(collector, "idle_seconds", lambda: 600)
    requests = []

    def offline(req):
        requests.append(req)
        raise httpx.ConnectError("offline", request=req)

    cfg = {"server": "https://server.test", "token": "test-token"}
    path = tmp_path / "config.json"
    queue = tmp_path / "queue.db"
    with httpx.Client(
        base_url=cfg["server"], transport=httpx.MockTransport(offline)
    ) as connection:
        outbox = collector.Outbox(queue)
        collector.run_cycle(cfg, path, connection, outbox)
        outbox.close()
        outbox = collector.Outbox(queue)
        collector.run_cycle(cfg, path, connection, outbox)
        outbox.close()
    received = []

    def online(req):
        if req.method == "GET":
            return httpx.Response(
                200,
                json={
                    "device_id": 42,
                    "sample_minutes": 1,
                    "idle_minutes": 3,
                    "skip_apps": [],
                },
            )
        events = json.loads(req.content)["events"]
        received.extend(events)
        return httpx.Response(
            200,
            json={
                "results": [
                    dict(client_event_id=e["client_event_id"], status="ok")
                    for e in events
                ]
            },
        )

    with httpx.Client(
        base_url=cfg["server"], transport=httpx.MockTransport(online)
    ) as connection:
        outbox = collector.Outbox(queue)
        collector.run_cycle(cfg, path, connection, outbox)
        assert outbox.con.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        outbox.close()
    assert len(received) == 3 and len({e["client_event_id"] for e in received}) == 3
    assert all(e["captured_at"] and e["idle_seconds"] == 600 for e in received)


def test_lost_acknowledgement_retries_same_id(tmp_path):
    outbox = collector.Outbox(tmp_path / "queue.db")
    event = dict(client_event_id="unique-event", captured_at="2026-10-09T10:00:00Z")
    outbox.enqueue("device", event)
    sent = []

    def handler(req):
        payload = json.loads(req.content)["events"]
        sent.append(payload)
        if len(sent) == 1:
            raise httpx.ReadError("lost ack", request=req)
        return httpx.Response(
            200,
            json={
                "results": [
                    dict(client_event_id="unrelated", status="ok"),
                    dict(client_event_id="unique-event", status="ok"),
                ]
            },
        )

    with httpx.Client(
        base_url="https://test", transport=httpx.MockTransport(handler)
    ) as client:
        assert collector.flush_outbox(client, outbox, "device") == 0
        assert outbox.count("device") == 1
        assert collector.flush_outbox(client, outbox, "device") == 1
    assert sent[0] == sent[1] and outbox.count("device") == 0
    outbox.close()


def test_other_channel_is_never_sent_or_acknowledged(tmp_path):
    outbox = collector.Outbox(tmp_path / "queue.db")
    outbox.enqueue("a", dict(client_event_id="a"))
    outbox.enqueue("b", dict(client_event_id="b"))
    outbox.acknowledge("a", [dict(client_event_id="b", status="ok")])
    assert outbox.count("a") == outbox.count("b") == 1
    outbox.acknowledge(
        "a", [dict(client_event_id="a", status="error", reason="bad time")]
    )
    assert outbox.pending("a") == [] and outbox.count("a") == 0
    assert (
        outbox.con.execute("SELECT error FROM events WHERE id=?", ("a",)).fetchone()[0]
        == "bad time"
    )
    assert outbox.pending("b") == [dict(client_event_id="b")]
    outbox.close()
