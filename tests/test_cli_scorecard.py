"""``omarketsctl scorecard``: reads the API, prints a plain text table."""

from core import cli
from core.desk import scorecard as SC
from tests.desk.test_scorecard import NOW, seeded


def test_scorecard_command_prints_the_table(monkeypatch, capsys):
    seen = {}

    def fake_get(settings, path):
        seen["path"] = path
        notes = seeded().between(NOW - 2 * SC.DEFAULT_WINDOW, NOW)
        card = SC.build(notes, now=NOW, start=NOW - SC.DEFAULT_WINDOW, end=NOW)
        return card.model_dump(mode="json")

    monkeypatch.setattr(cli, "_get", fake_get)
    assert cli.main(["scorecard"]) == 0
    out = capsys.readouterr().out
    assert seen["path"] == "/api/v1/desk/scorecard?compare=true"
    assert "This period" in out and "Previous period" in out and "desk:v11" in out


def test_scorecard_command_passes_the_range(monkeypatch, capsys):
    seen = {}

    def fake_get(settings, path):
        seen["path"] = path
        return SC.build([], now=NOW, start=NOW - SC.DEFAULT_WINDOW, end=NOW,
                        compare=False).model_dump(mode="json")

    monkeypatch.setattr(cli, "_get", fake_get)
    assert cli.main(["scorecard", "--from", "2026-10-01T00:00:00+00:00",
                     "--to", "2026-10-08T00:00:00+00:00", "--no-compare"]) == 0
    assert seen["path"] == ("/api/v1/desk/scorecard?from=2026-10-01T00%3A00%3A00%2B00%3A00"
                            "&to=2026-10-08T00%3A00%3A00%2B00%3A00&compare=false")
    assert "no notes in this period" in capsys.readouterr().out
