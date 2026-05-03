"""Tests for kavita.py — all HTTP calls are mocked."""
from unittest.mock import MagicMock, patch
from urllib import error as urlerror

from kavita import KavitaClient


def make_client():
    return KavitaClient("http://kavita:5000", "test-api-key")


def _mock_urlopen(body: bytes = b"null"):
    ctx = MagicMock()
    ctx.__enter__ = lambda s: s
    ctx.__exit__ = MagicMock(return_value=False)
    ctx.read.return_value = body
    return ctx


# ---------------------------------------------------------------------------
# Authentication header
# ---------------------------------------------------------------------------

def test_req_sends_api_key_header():
    client = make_client()
    with patch("kavita.urlrequest.urlopen", return_value=_mock_urlopen()) as mock_open:
        client._req("GET", "/api/Server/server-info")
    req = mock_open.call_args[0][0]
    assert req.get_header("X-api-key") == "test-api-key"


def test_req_sends_json_content_type_for_body():
    client = make_client()
    with patch("kavita.urlrequest.urlopen", return_value=_mock_urlopen()) as mock_open:
        client._req("POST", "/api/Upload/volume", body={"id": 1, "url": "http://x"})
    req = mock_open.call_args[0][0]
    assert req.get_header("Content-type") == "application/json"


def test_req_raises_runtime_error_on_http_error():
    client = make_client()
    err = urlerror.HTTPError("http://x", 403, "Forbidden", {}, None)
    err.read = lambda: b"denied"
    with patch("kavita.urlrequest.urlopen", side_effect=err):
        try:
            client._req("GET", "/api/test")
            assert False, "should have raised"
        except RuntimeError as e:
            assert "403" in str(e)


# ---------------------------------------------------------------------------
# ping
# ---------------------------------------------------------------------------

def test_ping_returns_true_on_success():
    client = make_client()
    with patch.object(client, "_req", return_value={}):
        assert client.ping() is True


def test_ping_returns_false_on_error():
    client = make_client()
    with patch.object(client, "_req", side_effect=RuntimeError("refused")):
        assert client.ping() is False


# ---------------------------------------------------------------------------
# scan_all
# ---------------------------------------------------------------------------

def test_scan_all_calls_correct_endpoint():
    client = make_client()
    with patch.object(client, "_req") as mock_req:
        client.scan_all()
    mock_req.assert_called_once_with("POST", "/api/Library/scan-all")


# ---------------------------------------------------------------------------
# search_series
# ---------------------------------------------------------------------------

def test_search_series_exact_name_match():
    client = make_client()
    results = {"series": [
        {"id": 1, "name": "Other Series", "localizedName": ""},
        {"id": 2, "name": "Isekai Ojisan", "localizedName": "Uncle from Another World"},
    ]}
    with patch.object(client, "_req", return_value=results):
        s = client.search_series("Isekai Ojisan")
    assert s["id"] == 2


def test_search_series_localized_name_match():
    client = make_client()
    results = {"series": [
        {"id": 5, "name": "Isekai Ojisan", "localizedName": "Uncle from Another World"},
    ]}
    with patch.object(client, "_req", return_value=results):
        s = client.search_series("Uncle from Another World")
    assert s["id"] == 5


def test_search_series_case_insensitive():
    client = make_client()
    results = {"series": [{"id": 3, "name": "My Series", "localizedName": ""}]}
    with patch.object(client, "_req", return_value=results):
        s = client.search_series("my series")
    assert s["id"] == 3


def test_search_series_not_found_returns_none():
    client = make_client()
    with patch.object(client, "_req", return_value={"series": []}):
        assert client.search_series("Nonexistent") is None


def test_search_series_empty_response_returns_none():
    client = make_client()
    with patch.object(client, "_req", return_value=None):
        assert client.search_series("Anything") is None


# ---------------------------------------------------------------------------
# get_volumes
# ---------------------------------------------------------------------------

def test_get_volumes_passes_series_id():
    client = make_client()
    with patch.object(client, "_req", return_value=[]) as mock_req:
        client.get_volumes(42)
    mock_req.assert_called_once_with("GET", "/api/Series/volumes", params={"seriesId": 42})


def test_get_volumes_returns_empty_on_none():
    client = make_client()
    with patch.object(client, "_req", return_value=None):
        assert client.get_volumes(1) == []


# ---------------------------------------------------------------------------
# set_series_cover / set_volume_cover
# ---------------------------------------------------------------------------

def test_set_series_cover_correct_payload():
    client = make_client()
    with patch.object(client, "_req") as mock_req:
        client.set_series_cover(10, "https://example.com/s.jpg")
    mock_req.assert_called_once_with(
        "POST", "/api/Upload/series", body={"id": 10, "url": "https://example.com/s.jpg"}
    )


def test_set_volume_cover_correct_payload():
    client = make_client()
    with patch.object(client, "_req") as mock_req:
        client.set_volume_cover(99, "https://uploads.mangadex.org/covers/x/y.512.jpg")
    mock_req.assert_called_once_with(
        "POST", "/api/Upload/volume",
        body={"id": 99, "url": "https://uploads.mangadex.org/covers/x/y.512.jpg"}
    )
