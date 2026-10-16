#!/usr/bin/env python3
"""Prevent accidental publication of host addresses, workstation paths and credentials.

Public DNS resolvers and documentation/private-network examples are allowed.
Only publishable branches/tags/remotes are scanned; editor recovery refs are local.
No matched value is printed, so CI output does not repeat a sensitive address.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
DNS_RESOLVERS = {"1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4", "9.9.9.9", "149.112.112.112"}
IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
HOST_PATH = re.compile(r"/(?:root|Users/[^/\s]+)/")
CREDENTIAL = re.compile(r"(?<!\w)(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{20,}|-{5}BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-{5}")


def git(*args, input=None):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], input=input)


def violations(content: str, private_values=()):
    found = []
    for number, line in enumerate(content.splitlines(), 1):
        for value in IPV4.findall(line):
            try:
                address = ipaddress.ip_address(value)
            except ValueError:
                continue
            if address.is_global and value not in DNS_RESOLVERS:
                found.append((number, "routable IPv4 literal; use external configuration or a documentation address"))
                break
        if HOST_PATH.search(line):
            found.append((number, "machine-specific home path; use a generic example"))
        if CREDENTIAL.search(line):
            found.append((number, "possible private credential"))
        if any(value and value.casefold() in line.casefold() for value in private_values):
            found.append((number, "value from private infrastructure configuration"))
    return found


def history_blobs():
    refs = git("for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags", "refs/remotes").decode().splitlines()
    if not refs:
        return
    objects = {}
    for entry in git("rev-list", "--objects", *refs).decode().splitlines():
        oid, _, name = entry.partition(" ")
        objects[oid] = name
    checked = git("cat-file", "--batch-check=%(objectname) %(objecttype)", input=("\n".join(objects) + "\n").encode())
    for entry in checked.decode().splitlines():
        oid, kind = entry.split()
        if kind == "blob":
            yield objects[oid] + "@" + oid[:10], git("cat-file", "blob", oid)
    yield "commit-messages", git("log", "--format=%B", *refs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true", help="also inspect all publishable Git history")
    parser.add_argument("--private-config", type=Path, help="optional private config; its values are never printed")
    args = parser.parse_args()
    private_values = []
    if args.private_config:
        config = json.loads(args.private_config.read_text())
        private_values.extend(config.get("private_domains", []))
        private_values.extend(config.get("documented_addresses", []))
    records = []
    for name in git("ls-files", "-z").decode().split("\0"):
        if name and (ROOT / name).is_file():
            records.append((name, (ROOT / name).read_bytes()))
    if args.history:
        records.extend(history_blobs())
    issues = 0
    seen = set()
    for name, raw in records:
        if raw in seen:
            continue
        seen.add(raw)
        try:
            content = raw.decode()
        except UnicodeError:
            continue
        for line, reason in violations(content, private_values):
            print(f"{name}:{line}: {reason}")
            issues += 1
    if issues:
        print(f"Publication check failed: {issues} finding(s). Matched values were hidden.")
        return 1
    print("Public content check passed" + (" (including Git history)." if args.history else "."))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
