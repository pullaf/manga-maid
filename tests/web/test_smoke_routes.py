"""
Tier A — route smoke (hermetic, no real library, no background job worker).

Goal: every handler returns *something* other than an unhandled 500. External/API-heavy
routes are listed under TIER_A_NETWORK for a follow-up (mock transport or --run-network).

Tier B (next): seed sqlite + fake CBZ, assert JSON bodies and DB mutations; patch shutil/os.remove
for delete-family routes.
"""

from __future__ import annotations

import pytest

# (method, path, allowed status codes)
TIER_A_HERMETIC: list[tuple[str, str, frozenset[int]]] = [
    ("GET", "/", frozenset({200})),
    ("GET", "/series", frozenset({200})),
    ("GET", "/jobs", frozenset({200})),
    ("GET", "/settings", frozenset({200})),
    ("GET", "/sources", frozenset({200})),
    ("GET", "/fix", frozenset({200})),
    ("GET", "/logs", frozenset({200})),
    # TestClient follows redirects by default → final /jobs is 200.
    ("GET", "/sync", frozenset({200, 307})),
    ("GET", "/api/sources", frozenset({200})),
    ("GET", "/api/jobs", frozenset({200})),
    ("GET", "/api/jobs/active", frozenset({200})),
    ("GET", "/api/jobs/999999", frozenset({404})),
    ("GET", "/series/does-not/exist", frozenset({404})),
]

# MangaDex / Suwayomi / search — need httpx mock or live flag (not run in default CI).
TIER_A_NETWORK: list[tuple[str, str]] = [
    ("GET", "/api/search?q=test"),
    ("GET", "/api/search/mdx-companion"),
    ("GET", "/api/manga/00000000-0000-0000-0000-000000000000/setup"),
    ("GET", "/api/manga/00000000-0000-0000-0000-000000000000/link-preview"),
    ("GET", "/api/manga/00000000-0000-0000-0000-000000000000/langs"),
    ("GET", "/api/manga/00000000-0000-0000-0000-000000000000/groups"),
]


@pytest.mark.parametrize("method,path,allowed", TIER_A_HERMETIC)
def test_smoke_hermetic_routes(web_client, method, path, allowed):
    r = web_client.request(method, path)
    assert r.status_code in allowed, f"{method} {path} -> {r.status_code} {r.text[:200]}"


def test_smoke_post_sync_all_enqueues(web_client):
    r = web_client.post("/api/jobs/sync-all")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True
    assert "job" in body


def test_smoke_post_reconcile_enqueues(web_client):
    r = web_client.post("/api/jobs/reconcile-disk")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True


def test_smoke_post_unlink_unknown_series(web_client):
    r = web_client.post("/api/series/nope/unlink")
    assert r.status_code == 404


def _seed_suwayomi_series(monkeypatch, *, path="library/Test", companion="mdx-uuid"):
    import db
    import web.app as appmod

    conn = appmod._get_conn()
    db.insert_series(
        conn,
        path=path,
        title="Test",
        language="en",
        start_chapter=0,
        source="suwayomi:123",
        source_id="456",
        mangadex_id=companion,
    )
    appmod._cover_cache.clear()
    return appmod


def test_dashboard_cover_prefers_suwayomi_thumbnail(web_client, monkeypatch):
    appmod = _seed_suwayomi_series(monkeypatch)

    class Client:
        def download_page(self, path):
            assert path == "/api/v1/manga/456/thumbnail"
            return b"native-cover"

    monkeypatch.setattr(appmod, "get_suwayomi_client", lambda: Client())
    monkeypatch.setattr(
        appmod._mdx, "_api_get",
        lambda *args, **kwargs: pytest.fail("MangaDex should not be queried"),
    )

    response = web_client.get("/api/series/library/Test/cover")
    assert response.status_code == 200
    assert response.json()["url"] == "/api/proxy/suwayomi/thumbnail/456"


def test_dashboard_cover_falls_back_to_mangadex_companion(web_client, monkeypatch):
    appmod = _seed_suwayomi_series(monkeypatch)

    class Client:
        def download_page(self, path):
            raise OSError("thumbnail unavailable")

    monkeypatch.setattr(appmod, "get_suwayomi_client", lambda: Client())
    monkeypatch.setattr(
        appmod._mdx,
        "_api_get",
        lambda path, params, timeout=15: {
            "data": {"relationships": [{
                "type": "cover_art",
                "attributes": {"fileName": "cover.jpg"},
            }]}
        },
    )

    response = web_client.get("/api/series/library/Test/cover")
    assert response.status_code == 200
    assert response.json()["url"] == "/api/proxy/cover/mdx-uuid/cover.jpg"


@pytest.mark.parametrize("method,path", TIER_A_NETWORK)
def test_smoke_network_routes_skipped(method, path):
    pytest.skip("Tier A network bucket: add httpx mock or pytest --run-network")
