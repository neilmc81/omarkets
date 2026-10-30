from datetime import UTC, datetime
from types import SimpleNamespace

from core import cli
from core.desk.models import DeskNote, NoteKind
from core.desk.store import DeskStore

AT = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)


def setup(monkeypatch, tmp_path):
    db = tmp_path / "data dir" / "desk.sqlite3"
    settings = SimpleNamespace(resolved_desk_db_path=db)
    monkeypatch.setattr(cli, "_service_settings", lambda: (settings, [], "", tmp_path))
    return db


def test_backup_command_copies_a_live_wal_database_and_restores(monkeypatch, tmp_path, capsys):
    db = setup(monkeypatch, tmp_path)
    live = DeskStore(db)  # stays open: rows may still be only in the WAL
    live.save(DeskNote(id="release:a", kind=NoteKind.RELEASE, title="CPI", created_at=AT,
                       status="failed"))
    live.save(DeskNote(id="release:a", kind=NoteKind.RELEASE, title="CPI", created_at=AT,
                       status="failed"))
    live.claim_failure_alert("release:a")
    live.claim_once("plan:2026-10-20", AT)
    out = tmp_path / "backups" / "copy.sqlite3"
    assert cli.main(["desk-backup", str(out)]) == 0
    assert "backed up 1 notes" in capsys.readouterr().out
    restored = DeskStore(out)
    assert restored.attempts("release:a") == 2
    assert not restored.claim_failure_alert("release:a")
    assert not restored.claim_once("plan:2026-10-20", AT)
    live.close()


def test_backup_command_refuses_missing_db_and_existing_target(monkeypatch, tmp_path, capsys):
    db = setup(monkeypatch, tmp_path)
    assert cli.main(["desk-backup", str(tmp_path / "x.sqlite3")]) == 1
    DeskStore(db).close()
    target = tmp_path / "exists.sqlite3"
    target.write_text("")
    assert cli.main(["desk-backup", str(target)]) == 1
    assert "already exists" in capsys.readouterr().err
