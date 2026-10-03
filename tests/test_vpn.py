"""Keys, traffic accounting, the install script, and the dashboard."""

from __future__ import annotations

import base64
import json
import os
import tempfile
import time
import zlib
from pathlib import Path

import pytest
import yaml

from app.services.awg.crypto import genkey, pubkey
from app.services.awg.lab import LabDaemon
from app.services.awg.manage import dashboard, issue_client, revoke_client
from app.services.awg.present import format_gib
from app.services.awg.protocol import (
    ALLOWED_IPS_LINE,
    new_server_config,
    render_client_conf,
    render_uapi_sync,
    render_vpn_uri,
    save_server_config,
)
from app.services.awg.provision import AMNEZIAWG_GO_COMMIT, build_plan, configure_script, install
from app.services.awg.store import Store
from app.services.awg.uapi import parse_uapi_dump

# A well-known WireGuard key pair. Locks the X25519 implementation.
_PRIV = "yAnz5TF+lXXJte14tji3zlMNq+hd2rYUIgJBgB3fBmk="
_PUB = "HIgo9xNzJMWLKASShiTqIybxZ0U3wGLiUeJ1PKf8ykw="


def test_wireguard_public_key_vector() -> None:
    assert pubkey(_PRIV) == _PUB


def test_vpn_uri_keeps_awg31_fields() -> None:
    cfg = new_server_config(listen_port=1234, address="10.66.66.1/24", mtu=1280, endpoint="203.0.113.10")
    private = genkey()
    conf = render_client_conf(
        cfg=cfg,
        client_private=private,
        client_address="10.66.66.2/32",
        endpoint="203.0.113.10:1234",
    )
    assert ALLOWED_IPS_LINE in conf
    uri = render_vpn_uri(conf, description="телефон")
    raw = uri.removeprefix("vpn://")
    raw += "=" * ((4 - len(raw) % 4) % 4)
    packed = base64.urlsafe_b64decode(raw)
    body = zlib.decompress(packed[4:])
    envelope = json.loads(body)
    last = json.loads(envelope["containers"][0]["awg"]["last_config"])
    assert envelope["defaultContainer"] == "amnezia-awg"
    assert last["HeaderProtectionKey"] == cfg.obfuscation.header_protection_key
    assert last["client_priv_key"] == private
    assert last["server_pub_key"] == cfg.public_key
    assert last["hostName"] == "203.0.113.10"


def test_uapi_dump_roundtrip() -> None:
    private = genkey()
    public = pubkey(private)
    from app.services.awg.crypto import b64_to_hex

    raw = (
        f"public_key={b64_to_hex(public)}\n"
        "endpoint=203.0.113.8:50000\n"
        "last_handshake_time_sec=10\n"
        "rx_bytes=5\n"
        "tx_bytes=9\n"
        "errno=0\n\n"
    )
    peers = parse_uapi_dump(raw)
    assert peers[0].public_key == public
    assert peers[0].rx_bytes == 5
    assert peers[0].tx_bytes == 9


def test_uapi_sync_ends_with_blank_line() -> None:
    cfg = new_server_config(listen_port=1234, address="10.66.66.1/24", mtu=1280, endpoint="203.0.113.10")
    blob = render_uapi_sync(cfg, [])
    assert blob.startswith("set=1\n")
    assert blob.endswith("\n\n")
    assert "replace_peers=true" in blob


def test_counter_baseline_and_daemon_restart() -> None:
    store = Store(Path(tempfile.mkdtemp()) / "panel.db")
    key = pubkey(genkey())
    assert store.apply_counter(key, 0, 100, 1) == (0, 0)
    assert store.state_map()[key] == (0, 100)
    assert store.samples_since(0) == []
    assert store.apply_counter(key, 0, 140, 2) == (0, 40)
    assert store.apply_counter(key, 0, 10, 3) == (0, 10)
    assert store.state_map()[key] == (0, 150)
    samples = store.samples_since(0)
    assert [row[3] for row in samples] == [40, 10]
    store.close()


def test_install_plan_pins_engine_and_keeps_keys() -> None:
    cfg = new_server_config(listen_port=1234, address="10.66.66.1/24", mtu=1280, endpoint="203.0.113.10")
    existing = yaml.safe_dump(cfg.to_yaml_dict(), sort_keys=False)
    plan = build_plan(existing_yaml=existing, peers_json="", port=2222, endpoint="198.51.100.8")
    saved = yaml.safe_load(plan.files["/etc/amneziawg/server.yml"])
    assert saved["private_key"] == cfg.private_key
    assert saved["listen_port"] == 2222
    assert saved["endpoint"] == "198.51.100.8"
    assert plan.endpoint == "198.51.100.8:2222"
    assert AMNEZIAWG_GO_COMMIT in plan.script
    script_cfg = configure_script(address="10.66.66.1/24", mtu=1280)
    assert "MASQUERADE" in script_cfg
    assert 'ethtool -K "$IFACE" tx off' in script_cfg
    assert 'if [ -S "$SOCK" ]; then' in script_cfg
    assert "icmp6-adm-prohibited" in script_cfg
    encoded = base64.b64encode(plan.files["/etc/amneziawg/server.yml"].encode()).decode()
    assert encoded in plan.script


class _MemoryRemote:
    def __init__(self, files: dict[str, str] | None = None, public_ip: str = "203.0.113.10"):
        self.files = files or {}
        self.public_ip = public_ip
        self.script = ""

    def run(self, command: str, timeout: int = 60) -> str:
        if "server.yml" in command:
            return self.files.get("server.yml", "")
        if "peers.json" in command:
            return self.files.get("peers.json", "")
        if "ifconfig.me" in command:
            return self.public_ip + "\n"
        return ""

    def run_script(self, script: str, timeout: int = 900) -> str:
        self.script = script
        return ""


def test_install_detects_endpoint_without_touching_existing_key() -> None:
    cfg = new_server_config(listen_port=1234, address="10.66.66.1/24", mtu=1280, endpoint="203.0.113.10")
    remote = _MemoryRemote({"server.yml": yaml.safe_dump(cfg.to_yaml_dict(), sort_keys=False)})
    endpoint = install(remote, port=1234, endpoint="")
    assert endpoint == "203.0.113.10:1234"
    assert cfg.private_key in base64.b64decode(_yaml_payload(remote.script)).decode()


def _yaml_payload(script: str) -> str:
    for line in script.splitlines():
        if line.startswith("printf '%s' ") and "server.yml" in line:
            payload = line.split("printf '%s' ", 1)[1].split(" | base64", 1)[0]
            return payload.strip().strip("'")
    raise AssertionError("server.yml embed missing")


@pytest.fixture
def world(tmp_path: Path):
    fd, name = tempfile.mkstemp(prefix="awg", suffix=".sock", dir="/tmp")
    os.close(fd)
    sock = Path(name)
    sock.unlink()
    awg_dir = tmp_path / "awg"
    awg_dir.mkdir()
    cfg = new_server_config(listen_port=1234, address="10.66.66.1/24", mtu=1280, endpoint="203.0.113.10")
    save_server_config(awg_dir / "server.yml", cfg)
    lab = LabDaemon(sock)
    lab.start()
    store = Store(tmp_path / "panel.db")
    try:
        yield awg_dir, sock, lab, store, cfg
    finally:
        lab.stop()
        store.close()


def test_issue_and_revoke_against_lab(world) -> None:
    awg_dir, sock, lab, store, _cfg = world
    row = issue_client(store, awg_dir, str(sock), "телефон", now=1_700_000_000)
    assert row.address == "10.66.66.2/32"
    assert "203.0.113.10:1234" in row.conf
    assert len(lab.peers) == 1
    now = int(time.time())
    lab.set_stats(row.public_key, rx=10, tx=2 * 1024**3, handshake=now)
    snap = dashboard(store, awg_dir, str(sock), now=now)
    assert snap["total_bytes"] == 2 * 1024**3 + 10
    assert snap["today_bytes"] == 0
    assert snap["online"] == 1
    snap = dashboard(store, awg_dir, str(sock), now=now + 1)
    lab.set_stats(row.public_key, rx=10, tx=3 * 1024**3, handshake=now)
    snap = dashboard(store, awg_dir, str(sock), now=now + 2)
    assert snap["total_bytes"] == 3 * 1024**3 + 10
    assert snap["today_bytes"] == 1024**3
    assert format_gib(snap["total_bytes"]).startswith("3.")
    revoke_client(store, awg_dir, str(sock), row.id, now=now + 3)
    assert lab.peers == {}
    assert store.get_client(row.id).revoked_at is not None
