import pytest


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    import app.db as db

    monkeypatch.setattr(db, "DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    for var in ("AMPLEMARKET_API_KEY", "ANTHROPIC_API_KEY", "APP_PASSWORD", "ADMIN_EMAIL", "ADMIN_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    db.init_db()
    yield
