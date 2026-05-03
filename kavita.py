import json
from urllib import error as urlerror
from urllib import parse, request as urlrequest


class KavitaClient:
    def __init__(self, base_url: str, api_key: str):
        self.base = base_url.rstrip("/")
        self.key = api_key

    def _req(self, method: str, path: str, body=None, params: dict = None):
        url = f"{self.base}{path}"
        if params:
            url += "?" + parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        req = urlrequest.Request(url, data=data, method=method)
        req.add_header("x-api-key", self.key)
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
            self._req("GET", "/api/Server/server-info")
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
                return s
        return None

    def get_volumes(self, series_id: int) -> list:
        return self._req("GET", "/api/Series/volumes", params={"seriesId": series_id}) or []

    def set_series_cover(self, series_id: int, image_url: str):
        self._req("POST", "/api/Upload/series", body={"id": series_id, "url": image_url})

    def set_volume_cover(self, volume_id: int, image_url: str):
        self._req("POST", "/api/Upload/volume", body={"id": volume_id, "url": image_url})
