import pytest


@pytest.fixture(autouse=True)
def no_cover_art_network(monkeypatch):
    """The runner looks for cover art (D46); tests never reach the Cover Art Archive."""
    from musiclib import art

    monkeypatch.setattr(art, "http_get", lambda url, **kw: None)
