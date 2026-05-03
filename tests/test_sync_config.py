import json
import sync_config


def test_load_defaults_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_config, "CONFIG_PATH", str(tmp_path / "nonexistent.json"))
    settings = sync_config.load_settings()
    assert settings == sync_config.DEFAULTS


def test_load_defaults_when_corrupt(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text("not json{{{")
    monkeypatch.setattr(sync_config, "CONFIG_PATH", str(path))
    settings = sync_config.load_settings()
    assert settings == sync_config.DEFAULTS


def test_save_and_reload(tmp_path, monkeypatch):
    path = str(tmp_path / "settings.json")
    monkeypatch.setattr(sync_config, "CONFIG_PATH", path)
    sync_config.save_settings({"volume_mode": True, "kavita_url": "http://kavita:5000"})
    settings = sync_config.load_settings()
    assert settings["volume_mode"] is True
    assert settings["kavita_url"] == "http://kavita:5000"


def test_save_merges_with_defaults(tmp_path, monkeypatch):
    path = str(tmp_path / "settings.json")
    monkeypatch.setattr(sync_config, "CONFIG_PATH", path)
    sync_config.save_settings({"volume_mode": True})
    settings = sync_config.load_settings()
    # All default keys still present
    for key in sync_config.DEFAULTS:
        assert key in settings
    assert settings["file_format"] == sync_config.DEFAULTS["file_format"]


def test_second_save_preserves_first(tmp_path, monkeypatch):
    path = str(tmp_path / "settings.json")
    monkeypatch.setattr(sync_config, "CONFIG_PATH", path)
    sync_config.save_settings({"kavita_url": "http://first"})
    sync_config.save_settings({"volume_mode": True})
    settings = sync_config.load_settings()
    assert settings["kavita_url"] == "http://first"
    assert settings["volume_mode"] is True


def test_default_naming_matches_mdx():
    assert sync_config.DEFAULTS["chapter_naming"] == "[%1 %2] %3 vol.%4 ch.%5"
    assert sync_config.DEFAULTS["volume_naming"] == "[%1 %2] %3 vol.%4"
