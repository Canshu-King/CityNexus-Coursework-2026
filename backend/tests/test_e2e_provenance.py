"""The authoritative frontend E2E gate trusts /health only if it echoes the
token of the run that started the server.  These tests pin both halves of that
promise: a server nobody stamped says so, and the value a suite passes through
its own process environment is the value the browser sees.

They also pin the boundary the pass-13 ruling drew: the provenance fields may
identify *which* throwaway database a run opened, but never the connection URI
itself — a field added for test provenance must not turn into a credential
exit the day DATABASE_URL carries a host or a password.
"""

from fastapi.testclient import TestClient
import pytest

import app.main as main
from app.core.config import Settings
from app.db.session import make_engine


def test_health_does_not_claim_a_run_id_it_was_never_given():
    body = TestClient(main.app).get("/api/v1/health").json()
    # A backend from another checkout/lane has no token, so it can never satisfy
    # the assertion the suite makes; the empty string is the safe default.
    assert body["e2e_run_id"] == ""
    assert body["e2e_database_id"] == ""


def test_health_echoes_the_token_of_the_process_that_started_it(monkeypatch):
    monkeypatch.setattr(main, "settings", Settings(e2e_run_id="run-20260919-p13"))
    body = TestClient(main.app).get("/api/v1/health").json()
    assert body["e2e_run_id"] == "run-20260919-p13"
    assert body["status"] == "ok"


def test_provenance_names_only_the_file_and_never_the_connection_string(monkeypatch):
    monkeypatch.setattr(
        main,
        "settings",
        Settings(e2e_run_id="run-x", database_url="sqlite:///./_run-x.db"),
    )
    body = TestClient(main.app).get("/api/v1/health").json()
    assert body["e2e_database_id"] == "_run-x.db"
    # No key may carry the URI, its scheme, or anything a future postgres DSN
    # could leak.
    assert "database_url" not in body
    for value in body.values():
        assert "sqlite:///" not in str(value)


def test_provenance_would_not_leak_a_credentialed_dsn(monkeypatch):
    """The regression this field replaced: a URI with a password used to be echoed whole."""
    monkeypatch.setattr(
        main,
        "settings",
        Settings(
            e2e_run_id="run-y",
            database_url="postgresql://citynexus:s3cr3t@db.internal:5432/citynexus",
        ),
    )
    body = TestClient(main.app).get("/api/v1/health").json()
    flattened = " ".join(str(value) for value in body.values())
    assert "s3cr3t" not in flattened
    assert "db.internal" not in flattened


def test_a_run_stamped_process_refuses_to_reopen_an_existing_database(tmp_path, monkeypatch):
    import app.db.session as session

    existing = tmp_path / "_run-existing.db"
    existing.write_bytes(b"not empty")
    monkeypatch.setattr(session, "settings", Settings(e2e_run_id="run-existing"))

    with pytest.raises(RuntimeError, match="refuses to reopen"):
        make_engine(f"sqlite:///{existing.as_posix()}")


def test_a_run_stamped_process_may_create_a_database_that_is_not_there_yet(tmp_path, monkeypatch):
    import app.db.session as session

    monkeypatch.setattr(session, "settings", Settings(e2e_run_id="run-fresh"))
    engine = make_engine(f"sqlite:///{(tmp_path / '_run-fresh.db').as_posix()}")
    assert engine.dialect.name == "sqlite"
    engine.dispose()


def test_an_unstamped_process_is_left_alone(tmp_path):
    """The demo server and the ordinary test suite keep their files; only an
    authoritative run is held to fresh-or-no-run."""
    import app.db.session as session

    existing = tmp_path / "citynexus.db"
    existing.write_bytes(b"rows")
    ambient_run_id = session.settings.e2e_run_id
    assert not ambient_run_id, "the ambient settings must not carry a run id"
    engine = make_engine(f"sqlite:///{existing.as_posix()}")
    assert engine.dialect.name == "sqlite"
    engine.dispose()
