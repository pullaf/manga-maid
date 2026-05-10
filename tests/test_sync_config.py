import json

import db
import file_permissions
import sync_config


def _isolated_data_dir(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(db, "DATA_DIR", str(root))
    monkeypatch.setattr(sync_config, "_settings_cache", None)
    monkeypatch.setattr(sync_config, "_settings_cache_at", 0.0)
    monkeypatch.delenv("CONFIG_PATH", raising=False)


def test_load_defaults_when_missing(tmp_path, monkeypatch):
    _isolated_data_dir(tmp_path, monkeypatch)
    settings = sync_config.load_settings()
    assert settings == sync_config.DEFAULTS


def test_load_defaults_when_corrupt_db_blob(tmp_path, monkeypatch):
    _isolated_data_dir(tmp_path, monkeypatch)
    conn = db.init_db()
    conn.execute(
        "UPDATE app_config SET settings_json = ? WHERE id = 1",
        ("not json{{{",),
    )
    conn.commit()
    conn.close()
    settings = sync_config.load_settings()
    assert settings == sync_config.DEFAULTS


def test_save_and_reload(tmp_path, monkeypatch):
    _isolated_data_dir(tmp_path, monkeypatch)
    sync_config.save_settings({"volume_mode": True, "kavita_url": "http://kavita:5000"})
    settings = sync_config.load_settings()
    assert "volume_mode" not in settings
    assert settings["kavita_url"] == "http://kavita:5000"


def test_save_merges_with_defaults(tmp_path, monkeypatch):
    _isolated_data_dir(tmp_path, monkeypatch)
    sync_config.save_settings({"volume_mode": True})
    settings = sync_config.load_settings()
    for key in sync_config.DEFAULTS:
        assert key in settings
    assert settings["file_format"] == sync_config.DEFAULTS["file_format"]


def test_second_save_preserves_first(tmp_path, monkeypatch):
    _isolated_data_dir(tmp_path, monkeypatch)
    sync_config.save_settings({"kavita_url": "http://first"})
    sync_config.save_settings({"volume_mode": True})
    settings = sync_config.load_settings()
    assert settings["kavita_url"] == "http://first"
    assert "volume_mode" not in settings


def test_default_naming_matches_mdx():
    assert sync_config.DEFAULTS["chapter_naming"] == "[%1 %2] %3 vol.%4 ch.%5"
    assert sync_config.DEFAULTS["volume_naming"] == "[%1 %2] %3 vol.%4"
    assert sync_config.DEFAULTS["file_permission_mask"] == "664"


def test_sanitize_file_permission_mask():
    assert file_permissions.sanitize_file_permission_mask("775") == "775"
    assert file_permissions.sanitize_file_permission_mask("0775") == "775"
    assert file_permissions.sanitize_file_permission_mask("0o755") == "755"
    assert file_permissions.sanitize_file_permission_mask("999") == "664"


def test_sanitize_volume_naming_removes_chapter_placeholders():
    assert sync_config.sanitize_volume_naming("%3 vol.%4 ch.%5") == "%3 vol.%4"
    assert sync_config.sanitize_volume_naming("[%1 %2] %3 vol.%4 ch.%5 %6") == (
        "[%1 %2] %3 vol.%4"
    )
    assert "%5" not in sync_config.sanitize_volume_naming("%3 vol.%4 %5")
    assert "%6" not in sync_config.sanitize_volume_naming("%3 vol.%4 %6")


def test_load_settings_sanitizes_volume_naming(tmp_path, monkeypatch):
    _isolated_data_dir(tmp_path, monkeypatch)
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"volume_naming": "%3 vol.%4 ch.%5"}))
    monkeypatch.setenv("CONFIG_PATH", str(legacy))
    s = sync_config.load_settings()
    assert "%5" not in s["volume_naming"]
    assert s["volume_naming"] == "%3 vol.%4"


def test_migrate_from_config_folder_json(tmp_path, monkeypatch):
    """Default legacy path ``DATA_DIR/config/settings.json`` is imported once."""
    _isolated_data_dir(tmp_path, monkeypatch)
    cfg_dir = tmp_path / "data" / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "settings.json").write_text(
        json.dumps({"kavita_url": "http://from-file", "volume_mode": True})
    )
    s = sync_config.load_settings()
    assert s["kavita_url"] == "http://from-file"
    assert "volume_mode" not in s
    assert not (cfg_dir / "settings.json").exists()
    assert (cfg_dir / "settings.json.migrated").exists()


def test_save_drops_merge_volume_naming(tmp_path, monkeypatch):
    _isolated_data_dir(tmp_path, monkeypatch)
    sync_config.save_settings({"merge_volume_naming": "%3 ch.%5", "kavita_url": "http://x"})
    conn = db.init_db()
    raw = json.loads(conn.execute(
        "SELECT settings_json FROM app_config WHERE id=1"
    ).fetchone()["settings_json"])
    conn.close()
    assert "merge_volume_naming" not in raw
    s = sync_config.load_settings()
    assert "merge_volume_naming" not in s
