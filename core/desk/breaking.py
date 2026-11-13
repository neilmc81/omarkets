"""Breaking-news alerts without AI: keyword rules over trusted headline sources.

Deliberately quiet. A headline alerts only when it comes from a trusted
source, matches a rule for a market-moving kind of event, is fresh, is not a
near-duplicate of an alert already sent, and the rate limit allows it. Rules
pair a topic with an action word ("imposes tariffs", not every story that
mentions tariffs), so routine coverage does not fire.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

#: Official US sources and major newsrooms. General world feeds that publish
#: hundreds of stories a week are excluded, as is OFAC's routine sanctions list.
TRUSTED_SOURCES = frozenset({
    "cnbc_top", "cnbc_markets", "cnbc_economy", "cnbc_tech", "bbc_world",
    "fed_press_monetary", "fed_press_all", "treasury_press", "whitehouse_actions",
    "federal_register_presidential", "state_press",
})
FED_SOURCES = frozenset({"fed_press_monetary", "fed_press_all"})

#: Only headlines published this recently can alert.
FRESH = timedelta(minutes=90)
#: At most one alert in this window, and :data:`DAILY_LIMIT` per 24 hours.
MIN_GAP = timedelta(minutes=30)
DAILY_LIMIT = 4
#: An alert this similar to one sent in the last :data:`DUPLICATE_WINDOW` is a repeat.
DUPLICATE_SIMILARITY = 0.5
DUPLICATE_WINDOW = timedelta(hours=12)


@dataclass(frozen=True)
class Rule:
    key: str
    label: str
    pattern: re.Pattern[str]
    #: When set, the rule only applies to headlines from these sources.
    sources: frozenset[str] | None = None


def _rx(text: str) -> re.Pattern[str]:
    return re.compile(text, re.IGNORECASE)


RULES: tuple[Rule, ...] = (
    Rule("fed_emergency", "Emergency Fed action", _rx(
        r"\b(emergency|unscheduled|intermeeting|inter-meeting)\b.{0,40}\b(rate|cut|hike|"
        r"meeting|action|move|facility|liquidity)\b|\b(rate|cut|hike|meeting|action)\b.{0,20}"
        r"\b(emergency|unscheduled|intermeeting)\b")),
    Rule("tariffs", "New tariffs", _rx(
        r"\b(impos\w*|announc\w*|slap\w*|rais\w*|hik\w*|doubl\w*|retaliat\w*|threaten\w*)"
        r"\s+(with\s+)?(new\s+|fresh\s+|sweeping\s+|higher\s+|retaliatory\s+|\d{1,3}\s?%\s+)*"
        r"tariffs?\b|"
        r"\btariffs?\s+(of|to)\s+\d{2,3}\s?%")),
    Rule("sanctions", "New sanctions", _rx(
        r"\b(impos\w*|announc\w*|slap\w*|unveil\w*)\b.{0,30}\bsanctions?\b.{0,40}\b(on|against)\b"
        r".{0,30}\b(china|russia|iran|chin\w*|russ\w*|iran\w*)\b")),
    Rule("conflict", "Attack or invasion", _rx(
        r"\b(invades?|invasion of|declares? war|blockade of taiwan|nuclear (test|strike|attack)|"
        r"(strikes?|attacks?|missiles?) on (taiwan|iran|israel|china|the us|the u\.s\.|america)|"
        r"(china|iran|israel|russia|us|u\.s\.) (strikes|attacks|bombs|invades) "
        r"(taiwan|iran|israel|china|us|u\.s\.|american))\b")),
    Rule("default", "Debt default or shutdown", _rx(
        r"\b(defaults? on (its )?(debt|bonds?|payments?)|debt default|sovereign default|"
        r"government shutdown (begins|starts)|shuts? down the government|debt ceiling breach)\b")),
    Rule("halt", "Trading halt", _rx(
        r"\b(market-?wide (trading )?halt|circuit breakers? (trip|trigger|halt)\w*|"
        r"halts? (all )?trading|trading (is )?halted|exchanges? (close|shut)\w*)\b")),
)


#: Explainers and opinion ("Why …", "How …", questions) are not breaking news.
_ANALYSIS = re.compile(r"^(why|how|what|analysis|opinion|explainer|live)\b|\?\s*$|\bpodcast\b",
                       re.IGNORECASE)


#: Conflict headlines about what might happen are not an event that happened.
_SPECULATION = re.compile(r"\b(possible|could|may|might|warns?|threat\w*|prepar\w*|plans?|"
                          r"considers?|weighs?|reportedly)\b", re.IGNORECASE)

#: ...nor is one the headline denies or calls unlikely. Deliberately narrow: a false
#: alert is cheap, suppressing a real attack is not. The hedge or denial must sit
#: right against the attack noun and end the clause ("attacks on Iran highly
#: unlikely", "Iran denies attacks on Israel"), and it must not follow a clause
#: connector or comma, so a second clause never vetoes an attack clause.
_ACT = r"(?:attacks?|strikes?|invasions?|war|military action)\b"
_TARGET = r"(?:\s+(?:on|against|of|in)\s+(?:the\s+)?[\w.’'-]+)?"
#: End of the clause: closing quote, full stop, a time phrase, or nothing.
_END = r"(?=\s*['\"”’]*\s*(?:$|[.!]|\s(?:this|next|today|tonight)\b))"
_HEDGE = (r"(?:(?:is|are|seems?)\s+)?(?:(?:highly|very)\s+)?(?:unlikely|not\s+(?:likely|expected))"
          r"(?:\s+to\s+(?:happen|occur|materiali[sz]e|take\s+place))?")
_DENIAL = re.compile(
    # "more US attacks on Iran 'highly unlikely'", "An invasion of Taiwan is unlikely to happen"
    rf"\b{_ACT}{_TARGET}\s+['\"“‘]?{_HEDGE}{_END}"
    # "no attack on Israel is planned"
    rf"|\bno\s+{_ACT}{_TARGET}\s+(?:(?:is|are)\s+)?(?:planned|expected|imminent){_END}"
    # "Israel rules out strikes on Iran", "China denies invasion of Taiwan"
    rf"|\b(?:rul(?:es|ed|ing)\s+out|denies|denied|no\s+plans?\s+(?:for|to)|no\s+intention\s+of|"
    rf"not\s+planning)\s+(?:(?:any|more|new|further|a|an|the|military)\s+){{0,2}}{_ACT}{_TARGET}{_END}"
    # "Iran won't attack Israel", "US will not launch an attack on Iran"
    r"|\b(?:will\s+not|won['’]t)\s+(?:attack|strike|invade)\b"
    rf"|\b(?:will\s+not|won['’]t)\s+(?:launch|carry\s+out)\s+(?:an?|any)\s+{_ACT}",
    re.IGNORECASE)

#: A denial after one of these belongs to a different clause than the attack.
_CLAUSE_BREAK = re.compile(r"[,;]|\b(as|after|and|but|while|amid|despite|though|although|if|"
                           r"when|before|since|with)\b", re.IGNORECASE)


def _denied(headline: str) -> bool:
    found = _DENIAL.search(headline)
    return bool(found) and not _CLAUSE_BREAK.search(headline[: found.start()])


def match_rule(headline: str, source: str) -> Rule | None:
    if source not in TRUSTED_SOURCES or _ANALYSIS.search(headline.strip()):
        return None
    for rule in RULES:
        if rule.sources is not None and source not in rule.sources:
            continue
        if rule.key == "fed_emergency" and source not in FED_SOURCES and not re.search(
            r"\b(fed|federal reserve|fomc)\b", headline, re.IGNORECASE
        ):
            continue
        if rule.key == "conflict" and (
            _SPECULATION.search(headline) or _denied(headline)
        ):
            continue
        if rule.pattern.search(headline):
            return rule
    return None


_STOP = frozenset(
    "the a an and or of to in on for at by with as is are was be from after over new says "
    "said will its it this that us u.s.".split()
)


def tokens(text: str) -> frozenset[str]:
    words = re.findall(r"[a-z0-9%]+", text.casefold())
    return frozenset(w for w in words if len(w) > 2 and w not in _STOP)


def similarity(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass(frozen=True)
class SentAlert:
    rule: str
    headline: str
    created_at: datetime


def is_repeat(headline: str, rule: str, sent: Iterable[SentAlert], *, now: datetime) -> bool:
    words = tokens(headline)
    for alert in sent:
        if now - alert.created_at > DUPLICATE_WINDOW:
            continue
        if similarity(words, tokens(alert.headline)) >= DUPLICATE_SIMILARITY:
            return True
        if alert.rule == rule and now - alert.created_at < timedelta(hours=2):
            return True  # the same kind of story, still developing
    return False


def rate_allows(sent: Sequence[SentAlert], *, now: datetime) -> bool:
    recent = [a for a in sent if now - a.created_at < timedelta(hours=24)]
    if len(recent) >= DAILY_LIMIT:
        return False
    return all(now - a.created_at >= MIN_GAP for a in recent)


#: Plain names for alert sources.
SOURCE_NAMES = {
    "cnbc_top": "CNBC", "cnbc_markets": "CNBC", "cnbc_economy": "CNBC", "cnbc_tech": "CNBC",
    "bbc_world": "BBC", "fed_press_monetary": "Federal Reserve", "fed_press_all": "Federal Reserve",
    "treasury_press": "US Treasury", "whitehouse_actions": "White House",
    "federal_register_presidential": "Federal Register", "state_press": "State Department",
}


__all__ = ["RULES", "TRUSTED_SOURCES", "SentAlert", "is_repeat", "match_rule", "rate_allows"]
