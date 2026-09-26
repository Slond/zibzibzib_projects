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
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
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


def front_window() -> tuple[str, str]:
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
            out = subprocess.check_output(
                ["osascript", "-e", script],
                text=True,
                errors="replace",
                timeout=8,
            )
            lines = out.splitlines()
            app = lines[0].strip() if lines else ""
            title = lines[1].strip() if len(lines) > 1 else ""
            return app, title
        if sys.platform == "win32":
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            user32.GetForegroundWindow.restype = ctypes.c_void_p
            user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
            user32.GetWindowTextLengthW.restype = ctypes.c_int
            user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
            user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_ulong]
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
                if kernel32.QueryFullProcessImageNameW(handle, 0, exe, ctypes.byref(size)):
                    app = Path(exe.value).stem
                kernel32.CloseHandle(handle)
            return app, buf.value
    except (OSError, subprocess.SubprocessError):
        return "", ""
    return "", ""


def send_sample(server: str, token: str, app: str, title: str, ssid: str):
    import httpx

    url = server.rstrip("/") + "/activity/api/ingest/" + token
    data = {
        "app_name": app,
        "window_title": title,
        "wifi_ssid": ssid,
    }
    try:
        response = httpx.post(url, data=data, timeout=30)
    except httpx.HTTPError as exc:
        print(f"Не отправилось: {exc}")
        return
    if response.status_code >= 400:
        print(f"Сервер отклонил событие: {response.status_code} {response.text[:200]}")
        return
    try:
        body = response.json()
    except ValueError:
        print(response.text[:200])
        return
    place = body.get("place") or "место не сопоставлено"
    shown = title or app or "окно"
    print(f"{body.get('status')} · {shown} · {place}")


def tick(server: str, token: str, remote: dict):
    skip = remote.get("skip_apps") or DEFAULT_SKIP
    idle_limit = max(1, int(remote.get("idle_minutes") or 3)) * 60
    idle = idle_seconds()
    if idle >= idle_limit:
        print(f"Простой {int(idle)} с, запись пропущена")
        return
    app, title = front_window()
    if is_skipped(app, skip):
        print(f"{app}: заголовок окна не отправляю")
        title = ""
    send_sample(server, token, app, title, wifi_ssid())

def fetch_config(server: str, token: str) -> dict | None:
    import httpx

    url = server.rstrip("/") + "/activity/api/config/" + token
    try:
        response = httpx.get(url, timeout=20)
    except httpx.HTTPError as exc:
        print(f"Сервер недоступен: {exc}")
        return None
    if response.status_code == 404:
        print("Сервер не узнал токен. Проверьте токен устройства.")
        return None
    if response.status_code >= 400:
        print(f"Настройки не прочитались: {response.status_code}")
        return None
    return response.json()



def main():
    if sys.platform not in ("darwin", "win32"):
        print("Клиент запускается на Mac или Windows, не на сервере.")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Клиент дневника активности")
    parser.add_argument("--server", help="https://ваш-домен")
    parser.add_argument("--token", help="токен устройства со страницы Активность")
    parser.add_argument("--config", default=str(config_path()))
    parser.add_argument("--once", action="store_true", help="один проход и выход")
    args = parser.parse_args()

    path = Path(args.config)
    cfg = load_config(path)
    if args.server:
        cfg["server"] = args.server.strip().rstrip("/")
    if args.token:
        cfg["token"] = args.token.strip()
    if args.server or args.token:
        save_config(path, cfg)
    if not cfg.get("server") or not cfg.get("token"):
        print("Укажите --server и --token. Они сохранятся в конфиг клиента.")
        sys.exit(2)

    while True:
        remote = fetch_config(cfg["server"], cfg["token"])
        if remote:
            try:
                tick(cfg["server"], cfg["token"], remote)
            except Exception as exc:
                print(f"Ошибка прохода: {exc}")
        if args.once:
            break
        minutes = 5
        if remote and remote.get("sample_minutes"):
            try:
                minutes = int(remote["sample_minutes"])
            except (TypeError, ValueError):
                minutes = 5
        time.sleep(max(60, minutes * 60))


if __name__ == "__main__":
    main()
