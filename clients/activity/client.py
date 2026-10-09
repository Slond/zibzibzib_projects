#!/usr/bin/env python3
"""Клиент дневника для Mac и Windows.

Запускается на вашем компьютере, не на сервере. Раз в несколько минут, пока вы
за машиной, отправляет активное приложение и заголовок окна. У браузера это
название открытой вкладки. Телефон этим скриптом не обслуживается.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_SKIP = [
    "1Password",
    "Bitwarden",
    "KeePass",
    "KeePassXC",
    "Messages",
    "Сообщения",
    "Telegram",
    "WhatsApp",
    "Signal",
    "Mail",
    "Почта",
    "Spark",
    "Outlook",
]


def config_path() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "activity-client"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", str(Path.home()))) / "activity-client"
    else:
        base = Path.home() / ".activity-client"
    return base / "config.json"


def load_config(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(path: Path, cfg: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def is_skipped(app: str, names: list[str]) -> bool:
    # Same rule as the server: short names match exactly, longer ones may be contained.
    folded = (app or "").casefold()
    if not folded:
        return False
    for name in names:
        token = name.strip().casefold()
        if not token:
            continue
        if folded == token or (len(token) >= 6 and token in folded):
            return True
    return False


def idle_seconds() -> float:
    try:
        if sys.platform == "darwin":
            out = subprocess.check_output(
                ["ioreg", "-c", "IOHIDSystem"],
                text=True,
                errors="replace",
                timeout=5,
            )
            for line in out.splitlines():
                if "HIDIdleTime" in line:
                    digits = "".join(ch for ch in line.split("=")[-1] if ch.isdigit())
                    if digits:
                        return int(digits) / 1_000_000_000
        elif sys.platform == "win32":

            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

            info = LASTINPUTINFO()
            info.cbSize = ctypes.sizeof(LASTINPUTINFO)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
                return 0.0
            tick = ctypes.windll.kernel32.GetTickCount()
            elapsed = (tick - info.dwTime) & 0xFFFFFFFF
            return elapsed / 1000.0
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0.0
    return 0.0


def wifi_ssid() -> str:
    try:
        if sys.platform == "darwin":
            for iface in ("en0", "en1"):
                try:
                    out = subprocess.check_output(
                        ["ipconfig", "getsummary", iface],
                        text=True,
                        errors="replace",
                        timeout=4,
                    )
                except (OSError, subprocess.SubprocessError):
                    continue
                for line in out.splitlines():
                    if "SSID" in line and "BSSID" not in line:
                        value = line.split(":", 1)[-1].strip()
                        if value and value.lower() != "none":
                            return value
        elif sys.platform == "win32":
            out = subprocess.check_output(
                ["netsh", "wlan", "show", "interfaces"],
                text=True,
                errors="replace",
                timeout=8,
            )
            for line in out.splitlines():
                stripped = line.strip()
                if stripped.startswith("SSID") and "BSSID" not in stripped:
                    return stripped.split(":", 1)[-1].strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    return ""


def front_window() -> tuple[str, str, str]:
    try:
        if sys.platform == "darwin":
            script = """
            tell application "System Events"
                set frontApp to name of first application process whose frontmost is true
                tell process frontApp
                    if (count of windows) > 0 then
                        set windowName to name of front window
                    else
                        set windowName to ""
                    end if
                end tell
            end tell
            return frontApp & linefeed & windowName
            """
            proc = subprocess.run(
                ["osascript", "-e", script],
                text=True,
                errors="replace",
                timeout=8,
                capture_output=True,
            )
            if proc.returncode != 0:
                err = (
                    proc.stderr or proc.stdout or "osascript завершился с ошибкой"
                ).strip()
                return "", "", err
            lines = proc.stdout.splitlines()
            app = lines[0].strip() if lines else ""
            title = lines[1].strip() if len(lines) > 1 else ""
            return app, title, ""
        if sys.platform == "win32":
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            user32.GetForegroundWindow.restype = ctypes.c_void_p
            user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
            user32.GetWindowTextLengthW.restype = ctypes.c_int
            user32.GetWindowTextW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_wchar_p,
                ctypes.c_int,
            ]
            user32.GetWindowThreadProcessId.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_ulong),
            ]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.OpenProcess.argtypes = [
                ctypes.c_uint,
                ctypes.c_bool,
                ctypes.c_ulong,
            ]
            kernel32.QueryFullProcessImageNameW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint,
                ctypes.c_wchar_p,
                ctypes.POINTER(ctypes.c_ulong),
            ]
            kernel32.QueryFullProcessImageNameW.restype = ctypes.c_bool
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            hwnd = user32.GetForegroundWindow()
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            handle = kernel32.OpenProcess(0x1000, False, pid.value)
            app = ""
            if handle:
                size = ctypes.c_ulong(32768)
                exe = ctypes.create_unicode_buffer(32768)
                if kernel32.QueryFullProcessImageNameW(
                    handle, 0, exe, ctypes.byref(size)
                ):
                    app = Path(exe.value).stem
                kernel32.CloseHandle(handle)
            return app, buf.value, ""
    except (OSError, subprocess.SubprocessError) as exc:
        return "", "", str(exc)
    return "", "", ""


def preview(server: str | None, token: str | None):
    print("Через 3 секунды сниму активное окно. Переключитесь на него.")
    time.sleep(3)
    idle = idle_seconds()
    app, title, err = front_window()
    ssid = wifi_ssid()
    skip = DEFAULT_SKIP
    idle_limit = 3 * 60
    if server and token:
        remote = fetch_config(server, token)
        if remote:
            skip = remote.get("skip_apps") or DEFAULT_SKIP
            try:
                idle_limit = max(1, int(remote.get("idle_minutes") or 3)) * 60
            except (TypeError, ValueError):
                idle_limit = 3 * 60
    sent_title = "" if is_skipped(app, skip) else title
    payload = {"app_name": app, "window_title": sent_title, "wifi_ssid": ssid}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"Простой: {int(idle)} с, порог {idle_limit} с")
    if err:
        print(f"Окно не прочиталось: {err}")
    elif idle >= idle_limit:
        print("Простой записывается и будет показан как неопределённость")
    elif not app and not sent_title:
        print("На сервер это не уйдёт: приложение и заголовок пустые")
    elif not sent_title and title:
        print(f"Заголовок скрыт, уйдёт только приложение: {app}")
    else:
        print("Именно это ушло бы на сервер.")


class Outbox:
    """Acknowledged SQLite outbox survives disconnection and process restarts."""

    def __init__(self, path):
        import sqlite3

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(path)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute(
            "CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, channel TEXT NOT NULL, payload TEXT NOT NULL, error TEXT)"
        )
        self.con.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_channel ON events(channel,error)"
        )
        self.con.commit()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def enqueue(self, channel, payload):
        with self.con:
            self.con.execute(
                "INSERT OR IGNORE INTO events(id,channel,payload) VALUES(?,?,?)",
                (
                    payload["client_event_id"],
                    channel,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

    def pending(self, channel, limit=100):
        return [
            json.loads(row[0])
            for row in self.con.execute(
                "SELECT payload FROM events WHERE channel=? AND error IS NULL ORDER BY rowid LIMIT ?",
                (channel, limit),
            )
        ]

    def acknowledge(self, channel, results):
        with self.con:
            for result in results:
                if result.get("status") in ("ok", "skipped"):
                    self.con.execute(
                        "DELETE FROM events WHERE channel=? AND id=?",
                        (channel, result["client_event_id"]),
                    )
                elif result.get("status") == "error":
                    self.con.execute(
                        "UPDATE events SET error=? WHERE channel=? AND id=?",
                        (
                            result.get("reason") or "rejected",
                            channel,
                            result["client_event_id"],
                        ),
                    )

    def reassign(self, old, new):
        with self.con:
            self.con.execute("UPDATE events SET channel=? WHERE channel=?", (new, old))

    def close(self):
        self.con.close()

    def count(self, channel):
        return self.con.execute(
            "SELECT COUNT(*) FROM events WHERE channel=? AND error IS NULL", (channel,)
        ).fetchone()[0]


def collect_sample(remote):
    from datetime import datetime, timezone
    import uuid

    app, title, err = front_window()
    if err or (not app and not title):
        print("Окно не прочиталось; проверьте доступ к названиям окон", flush=True)
        return None
    if is_skipped(app, remote.get("skip_apps") or DEFAULT_SKIP):
        title = ""
    return dict(
        client_event_id=uuid.uuid4().hex,
        captured_at=datetime.now(timezone.utc).isoformat(),
        app_name=app,
        window_title=title,
        wifi_ssid=wifi_ssid(),
        idle_seconds=idle_seconds(),
        sample_minutes=max(1, min(60, int(remote.get("sample_minutes") or 1))),
        actor="agent" if "computer use" in title.casefold() else "auto",
    )


def flush_outbox(client, outbox, channel, max_batches=4):
    import httpx

    total = 0
    for _ in range(max_batches):
        events = outbox.pending(channel)
        if not events:
            break
        try:
            response = client.post("/activity/api/batch", json={"events": events})
            response.raise_for_status()
            data = response.json()
            results = data["results"]
            if not isinstance(results, list):
                raise ValueError("bad response")
            submitted = {event["client_event_id"] for event in events}
            # Only explicit acknowledgements for this batch can remove a stored event.
            results = [
                r
                for r in results
                if isinstance(r, dict) and r.get("client_event_id") in submitted
            ]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            print(
                "Связи с сервером нет или запрос отклонён; события остаются в очереди",
                flush=True,
            )
            break
        outbox.acknowledge(channel, results)
        accepted = sum(r.get("status") in ("ok", "skipped") for r in results)
        total += accepted
        if any(r.get("status") == "error" for r in results):
            print(
                "Некоторые события отклонены и сохранены локально с причиной",
                flush=True,
            )
        if not accepted:
            break
    return total


def fetch_config(server, token, client=None):
    import httpx

    try:
        if client is None:
            with httpx.Client(
                base_url=server.rstrip("/"),
                headers={
                    "Authorization": "Bearer " + token,
                    "X-Activity-Client-Version": "2",
                },
                timeout=10,
            ) as connection:
                response = connection.get("/activity/api/config")
        else:
            response = client.get("/activity/api/config")
        response.raise_for_status()
        data = response.json()
        if (
            not isinstance(data, dict)
            or not 1 <= int(data.get("sample_minutes", 1)) <= 60
        ):
            raise ValueError("bad config")
        return data
    except (httpx.HTTPError, ValueError, TypeError):
        print("Настройки недоступны; использую последние сохранённые", flush=True)
        return None


def run_cycle(cfg, path, client, outbox, refresh=True):
    import hashlib

    def channel(identity):
        return hashlib.sha256(
            (cfg["server"] + "|" + str(identity)).encode()
        ).hexdigest()

    staging = channel(cfg["token"])
    binding = channel(cfg["token"])
    remote = (
        cfg.get("remote") if cfg.get("remote_binding") == binding else None
    ) or dict(sample_minutes=1, idle_minutes=3, skip_apps=DEFAULT_SKIP)
    if refresh:
        received = fetch_config(cfg["server"], cfg["token"], client)
        if received:
            remote = received
            cfg["remote"] = remote
            cfg["remote_binding"] = binding
            save_config(path, cfg)
    destination = channel(remote.get("device_id") or cfg["token"])
    if remote.get("device_id"):
        outbox.reassign(staging, destination)
    payload = collect_sample(remote)
    if payload:
        outbox.enqueue(destination, payload)
    flushed = flush_outbox(client, outbox, destination)
    print(
        f"Отправлено: {flushed}; ожидают отправки: {outbox.count(destination)}",
        flush=True,
    )
    return max(1, int(remote.get("sample_minutes") or 1)) * 60


def instance_lock(path):
    handle = Path(path).open("a+b")
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            handle.write(b"0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        handle.close()
        raise RuntimeError("Клиент уже запущен")
    return handle


def main():
    if sys.platform not in ("darwin", "win32"):
        print("Клиент запускается на Mac или Windows")
        return
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Клиент дневника активности")
    parser.add_argument("--server")
    parser.add_argument("--token")
    parser.add_argument("--config", default=str(config_path()))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--configure-only", action="store_true")
    args = parser.parse_args()
    path = Path(args.config)
    cfg = load_config(path)
    if args.server:
        cfg["server"] = args.server.strip().rstrip("/")
    if args.token:
        cfg["token"] = args.token.strip()
    if args.server or args.token:
        save_config(path, cfg)
    if args.configure_only:
        return
    if args.preview:
        preview(cfg.get("server"), cfg.get("token"))
        return
    if not cfg.get("server") or not cfg.get("token"):
        raise SystemExit("Укажите --server и --token")
    path.parent.mkdir(parents=True, exist_ok=True)
    with instance_lock(path.with_suffix(".lock")):
        import httpx

        outbox = Outbox(path.with_suffix(".queue.db"))
        try:
            with httpx.Client(
                base_url=cfg["server"],
                headers={
                    "Authorization": "Bearer " + cfg["token"],
                    "X-Activity-Client-Version": "2",
                },
                timeout=10,
            ) as client:
                refresh_at = 0
                while True:
                    begin = time.monotonic()
                    try:
                        interval = run_cycle(
                            cfg, path, client, outbox, refresh=begin >= refresh_at
                        )
                    except Exception as exc:
                        print(f"Ошибка прохода: {type(exc).__name__}", flush=True)
                        interval = 60
                    if begin >= refresh_at:
                        refresh_at = begin + 600
                    if args.once:
                        break
                    time.sleep(max(0, interval - (time.monotonic() - begin)))
        finally:
            outbox.close()


if __name__ == "__main__":
    main()
