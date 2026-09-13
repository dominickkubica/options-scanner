"""A page on another site cannot change anything through the local API (SEC-0002).

CORS only decides whether a browser shows a cross-origin response to the page that
asked; the request itself still runs. Reproduced before the fix: a POST to
/api/watchlist/ZZZZ with `Origin: https://evil.example` returned 200 and pinned the
symbol. These pin the refusal, and pin that the app itself, the Vite dev server and
non-browser clients can still write.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from optscan.api.app import create_app
from optscan.config import Settings
from optscan.storage import db

EVIL = {"Origin": "https://evil.example"}


@pytest.fixture
def client(tmp_settings: Settings) -> Iterator[TestClient]:
    tmp_settings.ensure_dirs()
    with TestClient(create_app(tmp_settings)) as test_client:
        yield test_client


def pinned(settings: Settings) -> list[str]:
    with db.session(settings.sqlite_path) as conn:
        return db.list_watchlist(conn)


def test_a_write_from_another_site_is_refused_and_changes_nothing(
    client: TestClient, tmp_settings: Settings
) -> None:
    response = client.post("/api/watchlist/ZZZZ", headers=EVIL)
    assert response.status_code == 403
    assert "ZZZZ" not in pinned(tmp_settings)


def test_a_forged_statement_never_reaches_the_ledger(client: TestClient) -> None:
    """text/plain is a CORS-safelisted type, so this one needed no preflight at all."""
    response = client.post(
        "/api/journal/import",
        content='"Activity Date","Description","Trans Code","Quantity","Price","Amount"\n',
        headers={**EVIL, "content-type": "text/plain"},
    )
    assert response.status_code == 403


def test_a_cross_site_fetch_without_an_origin_is_refused(client: TestClient) -> None:
    assert client.post("/api/refresh", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_the_app_the_dev_server_and_the_cli_can_still_write(
    client: TestClient, tmp_settings: Settings
) -> None:
    assert (
        client.post("/api/watchlist/QQQ", headers={"Origin": "http://testserver"}).status_code
        == 200
    )
    assert (
        client.post("/api/watchlist/DIA", headers={"Origin": "http://localhost:5173"}).status_code
        == 200
    )
    # No Origin at all: curl, the CLI, a script. Not a web page, so nothing to forge.
    assert client.post("/api/watchlist/IWM").status_code == 200
    assert {"QQQ", "DIA", "IWM"} <= set(pinned(tmp_settings))


def test_reads_are_left_alone(client: TestClient) -> None:
    assert client.get("/api/health", headers=EVIL).status_code == 200
