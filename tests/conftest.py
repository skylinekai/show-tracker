import pytest


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    import app.db as db

    monkeypatch.setattr(db, "DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    for var in ("AMPLEMARKET_API_KEY", "ANTHROPIC_API_KEY", "APP_PASSWORD", "ADMIN_EMAIL", "ADMIN_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    db.init_db()
    yield


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests never reach Amplemarket or Claude; anything not mocked fails fast instead of hanging."""
    from app import ai, amplemarket

    def blocked(*a, **kw):
        raise amplemarket.AmplemarketError("network disabled in tests")

    def blocked_ai(*a, **kw):
        raise ai.AIError("network disabled in tests")

    monkeypatch.setattr(amplemarket, "_request", blocked)
    monkeypatch.setattr(ai, "_call", blocked_ai)
