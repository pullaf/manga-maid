"""Settings-page behaviour for language preferences."""
# ---------------------------------------------------------------------------
# "Run a sync" prompt after a language change
# ---------------------------------------------------------------------------

def _save_settings(client, **overrides):
    form = {
        "root_folders_json": '["library"]', "file_format": "cbz",
        "chapter_naming": "%3 ch.%5", "volume_naming": "%3 vol.%4",
        "download_delay": "1.0", "sync_cron": "disabled",
        "file_permission_mask": "664", "telemetry_enabled": "false",
        "title_language": "", "cover_language": "", "filename_language": "",
    }
    form.update(overrides)
    return client.post("/api/settings", data=form, follow_redirects=False)


def test_changing_a_language_prompts_for_a_sync(web_client):
    r = _save_settings(web_client, title_language="en")
    assert r.status_code == 303
    assert "languages=changed" in r.headers["location"]
    page = web_client.get("/settings?languages=changed")
    assert "Language preferences saved" in page.text


def test_saving_unrelated_settings_does_not_prompt(web_client):
    r = _save_settings(web_client, download_delay="2.0")
    assert r.status_code == 303
    assert "languages=changed" not in r.headers["location"]
    assert "Language preferences saved" not in web_client.get("/settings").text


def test_resaving_the_same_language_does_not_prompt(web_client):
    _save_settings(web_client, title_language="en")
    r = _save_settings(web_client, title_language="en")
    assert "languages=changed" not in r.headers["location"]


def test_settings_page_has_the_unsaved_changes_bar(web_client):
    page = web_client.get("/settings").text
    assert 'id="unsaved-bar"' in page
    assert 'id="settings-form"' in page


def test_invalid_cron_keeps_previous_schedule_and_warns(web_client):
    import sync_config

    _save_settings(web_client, sync_cron="0 3 * * *")
    r = _save_settings(web_client, sync_cron="61 * * * *")
    assert "cron=invalid" in r.headers["location"]
    sync_config._settings_cache_at = 0.0
    assert sync_config.load_settings()["sync_cron"] == "0 3 * * *"
    assert "not a valid 5-field cron expression" in web_client.get("/settings?cron=invalid").text
