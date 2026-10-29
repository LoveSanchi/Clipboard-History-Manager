import pytest


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Never touch the real clipboard history, settings or tray socket."""
    for var, sub in (("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"),
                     ("XDG_RUNTIME_DIR", "run")):
        monkeypatch.setenv(var, str(tmp_path / sub))
    (tmp_path / "run").mkdir()
    monkeypatch.delenv("CLIP_KEEPER_SOCKET", raising=False)
