import base64
import json
from urllib import error as urlerror
from urllib import parse, request as urlrequest


class KavitaClient:
    def __init__(self, base_url: str, api_key: str):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self._token = None

    def _authenticate(self):
        url = f"{self.base}/api/Plugin/authenticate?" + parse.urlencode({
            "apiKey": self.key,
            "pluginName": "MangadexKavitaSync",
        })
        req = urlrequest.Request(url, method="POST")
        req.add_header("Content-Length", "0")
        with urlrequest.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        self._token = data["token"]

    def _req(self, method: str, path: str, body=None, params: dict = None):
        if self._token is None:
            self._authenticate()
        url = f"{self.base}{path}"
        if params:
            url += "?" + parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urlrequest.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self._token}")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urlrequest.urlopen(req, timeout=15) as resp:
                content = resp.read()
                return json.loads(content) if content.strip() else None
        except urlerror.HTTPError as e:
            body_text = e.read().decode(errors="replace")[:200]
            raise RuntimeError(f"Kavita {e.code} {method} {path}: {body_text}") from e

    def ping(self) -> bool:
        try:
            self._authenticate()
            return True
        except Exception:
            return False

    def scan_all(self):
        self._req("POST", "/api/Library/scan-all")

    def search_series(self, name: str) -> dict | None:
        results = self._req("GET", "/api/Search/search", params={"queryString": name}) or {}
        for s in results.get("series", []):
            if (s.get("name", "").lower() == name.lower()
                    or s.get("localizedName", "").lower() == name.lower()):
                return {"id": s["seriesId"], **s}
        return None

    def get_volumes(self, series_id: int) -> list:
        return self._req("GET", "/api/Series/volumes", params={"seriesId": series_id}) or []

    def _image_to_b64(self, image_url: str) -> str:
        with urlrequest.urlopen(image_url, timeout=15) as resp:
            return base64.b64encode(resp.read()).decode()

    def set_series_cover(self, series_id: int, image_url: str):
        self._req("POST", "/api/Upload/series", body={"id": series_id, "url": self._image_to_b64(image_url)})

    def set_volume_cover(self, volume_id: int, image_url: str):
        self._req("POST", "/api/Upload/volume", body={"id": volume_id, "url": self._image_to_b64(image_url)})
