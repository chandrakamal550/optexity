"""The local automation override sits on the real dispatch path.

If it fires by accident the worker silently runs a different automation than
the customer asked for, and the wrong result is customer-visible. A bare
relative filename against an unpinned cwd is not a strong enough guard for
that, so an explicit opt-in env var is required as well.
"""

from optexity.inference.child_process import (
    LOCAL_AUTOMATION_OVERRIDE_ENV,
    LOCAL_AUTOMATION_OVERRIDE_FILE,
    local_automation_override_path,
)


def _write_override(tmp_path):
    (tmp_path / LOCAL_AUTOMATION_OVERRIDE_FILE).write_text("{}")


def test_file_alone_does_not_fire(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(LOCAL_AUTOMATION_OVERRIDE_ENV, raising=False)
    _write_override(tmp_path)
    assert local_automation_override_path() is None


def test_env_var_alone_does_not_fire(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(LOCAL_AUTOMATION_OVERRIDE_ENV, "1")
    assert local_automation_override_path() is None


def test_both_conditions_fire(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(LOCAL_AUTOMATION_OVERRIDE_ENV, "1")
    _write_override(tmp_path)
    path = local_automation_override_path()
    assert path is not None
    assert path.name == LOCAL_AUTOMATION_OVERRIDE_FILE


def test_falsy_env_values_do_not_fire(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_override(tmp_path)
    for value in ("", "0", "false", "no", "off", "maybe"):
        monkeypatch.setenv(LOCAL_AUTOMATION_OVERRIDE_ENV, value)
        assert local_automation_override_path() is None, value


def test_truthy_env_spellings_all_fire(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_override(tmp_path)
    for value in ("1", "true", "TRUE", " yes ", "on"):
        monkeypatch.setenv(LOCAL_AUTOMATION_OVERRIDE_ENV, value)
        assert local_automation_override_path() is not None, value
