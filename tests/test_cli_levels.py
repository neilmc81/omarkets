"""``omarketsctl levels``: every level with its window in New York time."""

import io
import json
import urllib.error
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from core import cli
from core.analysis.levels import key_levels
from core.domain.models import MarketObservation


def at(*parts):
    return datetime(*parts, tzinfo=UTC)


def quote(ts, value):
    return MarketObservation(symbol="ES", timestamp=ts, value=value, unit="index points",
                             source="cnbc", received_at=ts + timedelta(minutes=10),
                             metadata={"feed_status": "delayed", "contract_code": "ESZ26"})


def serve(monkeypatch, payload):
    calls = []

    def get(settings, path):
        calls.append(path)
        return payload

    monkeypatch.setattr(cli, "_get", get)
    return calls


def test_levels_prints_the_monday_after_the_half_day(monkeypatch, memory_repo, capsys):
    memory_repo.store([quote(at(2026, 11, 27, 14, 30), 6_812.0),
                       quote(at(2026, 11, 27, 18, 10), 6_844.0)])
    es = SimpleNamespace(value=6_850.0, previous_close=6_842.0, contract_code="ESZ26",
                         feed_status="delayed")
    levels = key_levels("ES", es, memory_repo.ohlc, now=at(2026, 11, 30, 13, 0))
    calls = serve(monkeypatch, levels.model_dump(mode="json"))
    assert cli.main(["levels", "es", "--date", "2026-11-30"]) == 0
    assert calls == ["/api/v1/levels/es?date=2026-11-30"]
    out = capsys.readouterr().out
    assert "ES levels for trade date 2026-11-30  contract ESZ26  delayed feed" in out
    assert "as of Mon Nov 30 08:00 ET" in out
    lines = {line.split("  ")[0]: line for line in out.splitlines() if "  " in line}
    # Prior day is the short Friday: Thursday 18:00 ET to the 13:15 ET early close.
    assert "6,844.00" in lines["Prior day high"]
    assert "Thu Nov 26 18:00 -> Fri Nov 27 13:15 ET" in lines["Prior day high"]
    assert "Sun Nov 22 18:00 -> Fri Nov 27 13:15 ET" in lines["Prior week low"]
    assert "6,842.00" in lines["Prior settle"]
    assert "+8.00 (+0.12%)" in lines["Gap vs settle"]
    assert "(before the cash open)" in lines["Gap vs settle"]
    # Nothing stored for the overnight yet: shown as unavailable, with why.
    assert "unavailable" in lines["Overnight high"] and "(so far)" in lines["Overnight high"]
    assert "why: no stored quotes for this window" in out


def test_levels_without_a_date_asks_for_now(monkeypatch, memory_repo, capsys):
    levels = key_levels("ES", None, memory_repo.ohlc, now=at(2026, 11, 27, 17, 0))
    calls = serve(monkeypatch, levels.model_dump(mode="json"))
    assert cli.main(["levels", "ES"]) == 0
    assert calls == ["/api/v1/levels/ES"]
    assert "first cash price at Fri Nov 27 09:30 ET" in capsys.readouterr().out


def test_levels_rejects_a_bad_date_before_calling_the_api(capsys):
    with pytest.raises(SystemExit):
        cli.main(["levels", "ES", "--date", "27/11/2026"])


def test_an_api_error_shows_its_reason_not_cannot_reach(monkeypatch, capsys):
    def refuse(url, timeout):
        body = io.BytesIO(json.dumps({"detail": "2026-11-26 is not a trading day"}).encode())
        raise urllib.error.HTTPError(url, 422, "Unprocessable Entity", {}, body)

    monkeypatch.setattr(cli.urllib.request, "urlopen", refuse)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["levels", "ES", "--date", "2026-11-26"])
    assert exit_info.value.code == 2
    err = capsys.readouterr().err
    assert "422 2026-11-26 is not a trading day" in err
    assert "cannot reach" not in err
