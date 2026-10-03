"""Issue and revoke client keys, and build the dashboard from UAPI counters."""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from app.services.awg.crypto import genkey, pubkey
from app.services.awg.present import (
    ago,
    format_bytes,
    format_gib,
    format_rate,
    hourly_bars,
    start_of_local_day,
)
from app.services.awg.protocol import (
    AwgPeer,
    AwgPeerFile,
    client_endpoint,
    load_peers,
    load_server_config,
    next_client_address,
    render_client_conf,
    render_uapi_add_peer,
    render_uapi_remove_peer,
    render_uapi_sync,
    render_vpn_uri,
    save_peers,
)
from app.services.awg.store import ClientRow, Store
from app.services.awg.uapi import AwgUapiError, uapi_dump, uapi_send

ONLINE_SECONDS = 180
_NAME = re.compile(r"^[\w][\w ._-]{0,40}$", re.UNICODE)
_lock = threading.Lock()


class PanelError(Exception):
    pass


def validate_name(name: str) -> str:
    cleaned = name.strip()
    if not _NAME.match(cleaned):
        raise PanelError("Имя: буквы, цифры, пробел, точка или дефис, до 41 символа")
    return cleaned


def sync_files(store: Store, awg_dir: Path) -> None:
    cfg = load_server_config(awg_dir / "server.yml")
    if cfg is None:
        raise PanelError("AmneziaWG не установлен")
    peers = [
        AwgPeer(public_key=row.public_key, address=row.address, name=row.name)
        for row in store.list_clients(active_only=True)
    ]
    save_peers(awg_dir / "peers.json", AwgPeerFile(peers))
    blob = awg_dir / "uapi.set"
    blob.write_text(render_uapi_sync(cfg, peers))
    blob.chmod(0o600)


def import_existing_peers(store: Store, awg_dir: Path, now: int | None = None) -> None:
    """Peers already on disk (after install) show up even before the first key from the panel."""
    path = awg_dir / "peers.json"
    if not path.exists():
        return
    now = int(time.time()) if now is None else now
    for peer in load_peers(path).peers:
        if store.get_by_public_key(peer.public_key):
            continue
        store.insert_client(
            name=(peer.name or "импорт").strip() or "импорт",
            public_key=peer.public_key,
            address=peer.address,
            conf="",
            created_at=now,
        )


def issue_client(
    store: Store,
    awg_dir: Path,
    socket_path: str,
    name: str,
    now: int | None = None,
) -> ClientRow:
    name = validate_name(name)
    now = int(time.time()) if now is None else now
    cfg = load_server_config(awg_dir / "server.yml")
    if cfg is None:
        raise PanelError("AmneziaWG не установлен. Сначала выполните awg-panel install")
    endpoint = client_endpoint(cfg)
    with _lock:
        used = [row.address for row in store.list_clients(active_only=True)]
        address = next_client_address(cfg.address, used)
        private = genkey()
        public = pubkey(private)
        try:
            uapi_send(render_uapi_add_peer(public, address), socket_path)
        except AwgUapiError as exc:
            raise PanelError(str(exc)) from exc
        conf = render_client_conf(
            cfg=cfg,
            client_private=private,
            client_address=address,
            endpoint=endpoint,
        )
        row = store.insert_client(
            name=name,
            public_key=public,
            address=address,
            conf=conf,
            created_at=now,
        )
        try:
            sync_files(store, awg_dir)
        except Exception:
            store.revoke(row.id, now)
            try:
                uapi_send(render_uapi_remove_peer(public), socket_path)
            except AwgUapiError:
                pass
            raise
        return row


def revoke_client(
    store: Store,
    awg_dir: Path,
    socket_path: str,
    client_id: int,
    now: int | None = None,
) -> None:
    now = int(time.time()) if now is None else now
    row = store.get_client(client_id)
    if row is None or row.revoked_at is not None:
        raise PanelError("Клиент не найден")
    if load_server_config(awg_dir / "server.yml") is None:
        raise PanelError("AmneziaWG не установлен")
    with _lock:
        try:
            uapi_send(render_uapi_remove_peer(row.public_key), socket_path)
        except AwgUapiError as exc:
            raise PanelError(str(exc)) from exc
        store.revoke(client_id, now)
        sync_files(store, awg_dir)


def refresh(store: Store, socket_path: str, now: int | None = None) -> bool:
    now = int(time.time()) if now is None else now
    try:
        peers = uapi_dump(socket_path)
    except AwgUapiError:
        return False
    known = {row.public_key for row in store.list_clients()}
    for peer in peers:
        if peer.public_key in known:
            store.apply_counter(peer.public_key, peer.rx_bytes, peer.tx_bytes, now)
    return True


def client_link(conf: str, description: str) -> str:
    if not conf.strip():
        return ""
    return render_vpn_uri(conf, description=description)


def dashboard(store: Store, awg_dir: Path, socket_path: str, now: int | None = None) -> dict:
    now = int(time.time()) if now is None else now
    cfg = load_server_config(awg_dir / "server.yml")
    live: dict = {}
    alive = False
    if cfg is not None:
        try:
            for peer in uapi_dump(socket_path):
                live[peer.public_key] = peer
            alive = True
        except AwgUapiError:
            alive = False
        else:
            known = {row.public_key for row in store.list_clients()}
            for peer in live.values():
                if peer.public_key in known:
                    store.apply_counter(peer.public_key, peer.rx_bytes, peer.tx_bytes, now)

    day_start = start_of_local_day(now)
    chart_start = (now // 3600 - 23) * 3600
    samples = store.samples_since(min(day_start, chart_start))
    state = store.state_map()

    today_rx = today_tx = 0
    rate_rx = rate_tx = 0
    per_today: dict[str, list[int]] = {}
    per_rate: dict[str, list[int]] = {}
    chart_samples: list[tuple[int, int, int]] = []
    for public_key, ts, rx, tx in samples:
        if ts >= chart_start:
            chart_samples.append((ts, rx, tx))
        if ts >= day_start:
            today_rx += rx
            today_tx += tx
            bucket = per_today.setdefault(public_key, [0, 0])
            bucket[0] += rx
            bucket[1] += tx
        if ts >= now - 60:
            rate_rx += rx
            rate_tx += tx
            bucket = per_rate.setdefault(public_key, [0, 0])
            bucket[0] += rx
            bucket[1] += tx

    def view(row: ClientRow) -> dict:
        peer = live.get(row.public_key)
        rx_total, tx_total = state.get(row.public_key, (0, 0))
        today = per_today.get(row.public_key, [0, 0])
        recent = per_rate.get(row.public_key, [0, 0])
        handshake = int(peer.handshake) if peer else 0
        online = bool(peer) and handshake > 0 and (now - handshake) <= ONLINE_SECONDS
        if peer:
            seen = ago(handshake, now)
            endpoint = peer.endpoint or "—"
        elif not alive:
            seen = "нет связи с демоном"
            endpoint = "—"
        else:
            seen = "не подключался"
            endpoint = "—"
        return {
            "id": row.id,
            "name": row.name,
            "address": row.address.split("/", 1)[0],
            "online": online,
            "handshake": seen,
            "endpoint": endpoint,
            "today": format_bytes(today[0] + today[1]),
            "total": format_bytes(rx_total + tx_total),
            "down": format_bytes(tx_total),
            "up": format_bytes(rx_total),
            "rate": format_rate((recent[0] + recent[1]) / 60),
            "revoked": row.revoked_at is not None,
            "has_conf": bool(row.conf.strip()),
        }

    rows = store.list_clients()
    active = [view(row) for row in rows if row.revoked_at is None]
    revoked = [view(row) for row in rows if row.revoked_at is not None]
    online = sum(1 for row in active if row["online"])
    total_rx = sum(pair[0] for pair in state.values())
    total_tx = sum(pair[1] for pair in state.values())
    chart = hourly_bars(chart_samples, now)
    total_bytes = total_rx + total_tx
    today_bytes = today_rx + today_tx
    return {
        "deployed": cfg is not None,
        "alive": alive,
        "endpoint": f"{cfg.endpoint}:{cfg.listen_port}" if cfg and cfg.endpoint else "",
        "listen_port": cfg.listen_port if cfg else 0,
        "online": online,
        "active": len(active),
        "online_label": f"{online} из {len(active)}",
        "today_bytes": today_bytes,
        "total_bytes": total_bytes,
        "today_gib": format_gib(today_bytes),
        "today_exact": format_bytes(today_bytes),
        "total_gib": format_gib(total_bytes),
        "total_exact": format_bytes(total_bytes),
        "split": (
            f"↓ {format_bytes(total_tx)} скачано клиентами · "
            f"↑ {format_bytes(total_rx)} отправлено клиентами"
        ),
        "rate": format_rate((rate_rx + rate_tx) / 60),
        "chart": chart,
        "chart_empty": sum(bar["total"] for bar in chart) == 0,
        "clients": active,
        "revoked": revoked,
    }
