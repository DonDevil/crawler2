"""P4 interception hook served by the P6 filter (design §12)."""

from __future__ import annotations

from datetime import UTC, datetime

from crawler2.crawlers.interception import InterceptAction, InterceptedRequest
from crawler2.filtering.intercept import FilterInterceptor
from crawler2.filtering.model import Classification, Party, Policy, Rule, RuleKind, RuleSource
from crawler2.filtering.store import RulesetHolder, publish, store_source
from tests.unit.filtering.fakes import MemoryFilterRules
from tests.unit.filtering.test_store import _item

T0 = datetime(2026, 9, 29, tzinfo=UTC)


def _holder() -> RulesetHolder:
    repo = MemoryFilterRules()
    rules = [
        Rule(
            source=RuleSource.EASYLIST,
            kind=RuleKind.HOST,
            pattern="ads.test",
            classification=Classification.AD,
            confidence=0.95,
        ),
        Rule(
            source=RuleSource.EASYPRIVACY,
            kind=RuleKind.URL_PATTERN,
            pattern="/pixel.gif",
            classification=Classification.TRACKER,
            confidence=0.95,
            party=Party.THIRD,
        ),
    ]
    revision = store_source(repo, _item(rules, RuleSource.EASYLIST), by="t", at=T0)
    record, _ = publish(repo, [("easylist", revision.revision)], Policy(), by="t", at=T0)
    repo.activate(record.ruleset_id, expected=None, by="t", at=T0)
    holder = RulesetHolder(repo)
    holder.refresh()
    return holder


def _req(url: str, kind: str = "script", **kw: object) -> InterceptedRequest:
    fields: dict[str, object] = {
        "url": url,
        "resource_type": kind,
        "is_navigation": False,
        "frame_url": "https://site.test/",
        "method": "GET",
        "page_url": "https://site.test/",
    }
    fields.update(kw)
    return InterceptedRequest(**fields)  # type: ignore[arg-type]


def test_ad_and_tracker_subrequests_are_blocked_with_explanation() -> None:
    hook = FilterInterceptor(_holder())
    ad = hook.decide(_req("https://cdn.ads.test/a.js"))
    assert (ad.action, ad.classification, ad.reason) == (InterceptAction.BLOCK, "ad", None)
    assert ad.rule_id
    assert ad.rule_id.startswith("easylist/host/")
    assert ad.ruleset
    assert ad.ruleset.startswith("rs-")
    pixel = hook.decide(_req("https://stats.other.test/pixel.gif", "image"))
    assert (pixel.action, pixel.classification) == (InterceptAction.BLOCK, "tracker")
    first_party = hook.decide(_req("https://www.site.test/pixel.gif", "image"))
    assert first_party.action is InterceptAction.ALLOW


def test_main_frame_navigation_is_never_blocked_but_is_classified() -> None:
    hook = FilterInterceptor(_holder())
    nav = hook.decide(
        _req("https://ads.test/landing", "document", is_navigation=True, is_main_frame=True)
    )
    assert (nav.action, nav.classification) == (InterceptAction.ALLOW, "ad")
    iframe = hook.decide(_req("https://ads.test/frame", "document", is_navigation=True))
    assert iframe.action is InterceptAction.BLOCK


def test_non_web_urls_pass_untouched() -> None:
    hook = FilterInterceptor(_holder())
    decision = hook.decide(_req("data:image/png;base64,AAAA", "image"))
    assert (decision.action, decision.classification) == (InterceptAction.ALLOW, None)


def test_without_an_active_ruleset_everything_is_allowed() -> None:
    hook = FilterInterceptor(RulesetHolder(MemoryFilterRules()))
    decision = hook.decide(_req("https://ads.test/a.js"))
    assert (decision.action, decision.ruleset) == (InterceptAction.ALLOW, "none")
