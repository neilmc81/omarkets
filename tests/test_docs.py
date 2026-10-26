"""Docs must match the product's quote labelling.

Quotes are labelled live or delayed; a shut market is presented as
"Markets closed", never as a stale quote. These tests pin the wording so the
user-facing docs cannot drift back to the old three-label list.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_readme_no_longer_lists_stale_as_quote_label():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert not re.search(r"\*\*live\*\*, \*\*delayed\*\* or \*\*stale\*\*", text), (
        "README still lists stale as a normal quote label"
    )
    assert "Markets closed" in text, (
        "README does not say the UI shows Markets closed for a shut market"
    )


def test_plugin_readme_not_out_of_session_stale():
    text = (ROOT / "omarchy" / "omarkets" / "README.md").read_text(encoding="utf-8")
    assert "stale / out of session" not in text, (
        "plugin README still presents out of session as stale"
    )
    assert re.search(r"^\| CLOSED \|", text, flags=re.MULTILINE), (
        "plugin README status table does not list CLOSED for a shut market"
    )


def test_reference_docs_say_markets_closed():
    text = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
    assert "Markets closed" in text, (
        "reference does not mention the Markets closed presentation"
    )
    assert "an out-of-session value `STALE`" not in text, (
        "reference still labels an out-of-session value stale"
    )