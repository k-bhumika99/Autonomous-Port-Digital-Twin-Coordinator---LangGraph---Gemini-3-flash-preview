"""Shared pytest fixtures: a fresh app + temporary SQLite file per test. No Gemini key is ever used."""
import copy
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import TestConfig  # noqa: E402
from app import create_app  # noqa: E402


@pytest.fixture()
def sample_port():
    return json.loads((ROOT / "data" / "sample_port.json").read_text())


@pytest.fixture()
def sample_scenarios():
    return {s["scenario"]["kind"]: s["scenario"] for s in json.loads((ROOT / "data" / "sample_scenarios.json").read_text())}


@pytest.fixture()
def app(tmp_path):
    class C(TestConfig):
        DATABASE = str(tmp_path / "test.sqlite3")
    return create_app(C)


@pytest.fixture()
def client(app):
    return app.test_client()


def register_and_login(client, email="a@example.com", name="Ann Tester", password="Passw0rd!x"):
    client.post("/signup", data={"full_name": name, "email": email, "password": password, "confirm_password": password})
    return client.post("/signin", data={"email": email, "password": password})


@pytest.fixture()
def logged_in(client):
    register_and_login(client)
    return client
