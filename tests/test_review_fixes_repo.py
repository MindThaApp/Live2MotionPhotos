"""Repo hygiene from the 2026-11-09 review: safe example config, secrets/state not committed or baked into the image."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def lines(name):
    return {x.strip() for x in (ROOT / name).read_text(encoding="utf-8").splitlines()}


def test_example_config_is_non_destructive():
    cfg = json.loads((ROOT / "config.example.json").read_text(encoding="utf-8"))
    assert cfg["delete_video"] is False and cfg["overwrite"] is False


def test_state_and_secrets_are_ignored():
    assert {"preview.json", "*.tmp", ".env", ".port", "config.json"} <= lines(".gitignore")
    assert {".env", "preview.json", ".port", "config.json"} <= lines(".dockerignore")


def test_dockerfile_has_no_world_writable_dirs():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "chmod 777" not in text and "chown ${UID}:${GID}" in text


def test_config_tmp_is_ignored():
    assert "config.json.tmp" in lines(".gitignore") and "config.json.tmp" in lines(".dockerignore")
