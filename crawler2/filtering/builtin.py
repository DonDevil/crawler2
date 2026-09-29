"""The ``built_in`` source: V1 knowledge worth keeping, as reviewed data (design §8.3).

No content-source or target hosts are listed here. Protective rules for
known content/media sources are operator rules with their own provenance.
"""

from __future__ import annotations

import hashlib
import json
from typing import Final

from crawler2.filtering.model import (
    Action,
    Classification,
    Context,
    Rule,
    RuleKind,
    RuleSource,
)

AD_NETWORKS: Final = (
    "adnxs.com",
    "doubleclick.net",
    "exoclick.com",
    "googleadservices.com",
    "googlesyndication.com",
    "mgid.com",
    "outbrain.com",
    "popads.net",
    "propellerads.com",
    "taboola.com",
)
"""V1 ``AUTO_BLACKLIST_DEFAULTS`` ad networks (audit F-1)."""

OUT_OF_SCOPE: Final = (
    "bookmyshow.com",
    "facebook.com",
    "hindustantimes.com",
    "imdb.com",
    "indianexpress.com",
    "indiatoday.in",
    "instagram.com",
    "justwatch.com",
    "letterboxd.com",
    "linkedin.com",
    "metacritic.com",
    "msn.com",
    "ndtv.com",
    "news18.com",
    "pinterest.com",
    "quora.com",
    "reddit.com",
    "rottentomatoes.com",
    "t.me",
    "telegram.org",
    "thehindu.com",
    "tiktok.com",
    "twitter.com",
    "wikidata.org",
    "wikimedia.org",
    "wikipedia.org",
    "x.com",
    "youtu.be",
    "youtube.com",
)
"""V1 ``AUTO_BLACKLIST_DEFAULTS`` reference/news/social sites: content, not crawl targets."""

AD_HOST_LABELS: Final = (
    "adclick",
    "adserver",
    "adservice",
    "adsystem",
    "adtrack",
    "advert",
    "banner",
    "popunder",
    "popup",
)
"""V1 ``AD_HOST_HINT_PATTERNS[0]`` as whole host labels (never the TLD); label only."""

LABEL_CONFIDENCE: Final = 0.6
"""Below the default block threshold: a label hint classifies, it never blocks."""


def built_in_rules() -> list[Rule]:
    rules = [
        Rule(
            source=RuleSource.BUILT_IN,
            kind=RuleKind.HOST,
            pattern=host,
            classification=Classification.AD,
            confidence=1.0,
            reason="ad_network",
            origin="crawler2/filtering/builtin.py AD_NETWORKS (V1 AUTO_BLACKLIST_DEFAULTS)",
        )
        for host in AD_NETWORKS
    ]
    rules += [
        Rule(
            source=RuleSource.BUILT_IN,
            kind=RuleKind.HOST,
            pattern=host,
            classification=Classification.CONTENT,
            confidence=1.0,
            action=Action.BLOCK,
            contexts=frozenset({Context.LINK}),
            reason="out_of_scope",
            origin="crawler2/filtering/builtin.py OUT_OF_SCOPE (V1 AUTO_BLACKLIST_DEFAULTS)",
        )
        for host in OUT_OF_SCOPE
    ]
    rules += [
        Rule(
            source=RuleSource.BUILT_IN,
            kind=RuleKind.HOST_LABEL,
            pattern=label,
            classification=Classification.AD,
            confidence=LABEL_CONFIDENCE,
            reason="ad_host_label",
            origin="crawler2/filtering/builtin.py AD_HOST_LABELS (V1 AD_HOST_HINT_PATTERNS[0])",
        )
        for label in AD_HOST_LABELS
    ]
    return sorted(rules, key=lambda r: r.rule_id)


def built_in_revision() -> str:
    """Content digest of the built-in rule set (its source revision)."""
    docs = [r.to_doc() for r in built_in_rules()]
    return hashlib.sha256(json.dumps(docs, sort_keys=True).encode()).hexdigest()[:16]
