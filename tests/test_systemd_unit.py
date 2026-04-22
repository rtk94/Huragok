"""Validate the shipped systemd unit file.

We don't start any real service; we just parse ``scripts/systemd/huragok.service``
with :mod:`configparser` and assert that the keys ADR-0002 D8 calls out
are present. The unit is an artifact, not an install target.
"""

from __future__ import annotations

import configparser
from pathlib import Path

SERVICE_PATH = Path(__file__).resolve().parent.parent / "scripts" / "systemd" / "huragok.service"


def _load_unit() -> configparser.ConfigParser:
    # Systemd unit files permit duplicate keys in some sections; we
    # turn strict=False so configparser accepts what systemd does.
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.read(SERVICE_PATH, encoding="utf-8")
    return parser


def test_unit_file_ships_at_expected_path() -> None:
    assert SERVICE_PATH.exists(), f"systemd unit missing at {SERVICE_PATH}"


def test_unit_file_parses_cleanly() -> None:
    parser = _load_unit()
    assert "Unit" in parser.sections()
    assert "Service" in parser.sections()
    assert "Install" in parser.sections()


def test_unit_service_fields_match_adr_0002_d8() -> None:
    parser = _load_unit()
    service = parser["Service"]
    assert service["Type"] == "notify"
    assert service["WorkingDirectory"] == "%h/huragok-runtime"
    assert service["EnvironmentFile"] == "%h/.config/huragok/huragok.env"
    assert service["ExecStart"].endswith("huragok run")
    assert service["Restart"] == "on-failure"
    assert service["KillSignal"] == "SIGTERM"
    assert service["KillMode"] == "mixed"


def test_unit_hardening_fields() -> None:
    parser = _load_unit()
    service = parser["Service"]
    assert service["ProtectSystem"] == "strict"
    assert service["ProtectHome"] == "read-only"
    assert service["PrivateTmp"] == "true"
    assert service["NoNewPrivileges"] == "true"
    assert service["ReadWritePaths"] == "%h/huragok-runtime"


def test_unit_install_section() -> None:
    parser = _load_unit()
    install = parser["Install"]
    assert install["WantedBy"] == "default.target"


def test_unit_references_adr_0001() -> None:
    # Documentation link should survive future refactors — it's the
    # main entry point operators hit from `systemctl cat`.
    parser = _load_unit()
    unit = parser["Unit"]
    assert "ADR-0001" in unit["Documentation"]
