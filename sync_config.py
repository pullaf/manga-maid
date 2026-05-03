import json
import os

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/config/settings.json")

DEFAULTS = {
    "root_folders": [],
    "file_format": "cbz",
    "chapter_naming": "[%1 %2] %3 vol.%4 ch.%5",
    "volume_naming": "[%1 %2] %3 vol.%4",
    "download_delay": 1.0,
    "volume_mode": False,
    "auto_covers": False,
    "auto_scan": False,
    "kavita_url": "",
    "kavita_api_key": "",
}


def load_settings() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return {**DEFAULTS, **json.load(f)}
    except Exception:
        return dict(DEFAULTS)


def save_settings(data: dict):
    current = load_settings()
    merged = {**current, **data}
    os.makedirs(os.path.dirname(CONFIG_PATH) or ".", exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(merged, f, indent=2)
