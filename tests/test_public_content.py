"""Publication rules must catch real host values without rejecting portable examples."""
import runpy
from pathlib import Path

RULES = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/check_public_content.py"))


def test_publication_rule_rejects_routable_host_and_home_paths():
    address = ".".join(map(str, [45, 1, 2, 3]))
    home = "/" + "root" + "/private-app"
    findings = RULES["violations"](address + "\n" + home)
    assert [line for line, _ in findings] == [1, 2]
    assert all(address not in reason and home not in reason for _, reason in findings)


def test_publication_rule_accepts_dns_and_documentation_examples():
    assert RULES["violations"]("1.1.1.1 8.8.8.8 203.0.113.10 192.0.2.1 127.0.0.1 10.0.0.1 /opt/app") == []


def test_publication_rule_uses_private_values_without_printing_them():
    value = "private-host.example.test"
    findings = RULES["violations"]("https://" + value, [value])
    assert len(findings) == 1 and value not in findings[0][1]
