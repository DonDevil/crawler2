"""P6 implementation of the P4 ``RequestInterceptor`` hook (design §12).

The browser still fetches; this only answers ALLOW or BLOCK per request
from the active ruleset and reports the classification so P4 can aggregate
it per page. A main-frame navigation is never blocked (the page was
admitted by the frontier); its decision is still counted. Nothing here
defeats or evades any protection.
"""

from __future__ import annotations

from crawler2.crawlers.interception import (
    ALLOW,
    InterceptAction,
    InterceptDecision,
    InterceptedRequest,
)
from crawler2.filtering.inputs import request_input
from crawler2.filtering.model import Action
from crawler2.filtering.store import RulesetHolder


class FilterInterceptor:
    def __init__(self, holder: RulesetHolder) -> None:
        self._holder = holder

    def decide(self, request: InterceptedRequest) -> InterceptDecision:
        page_url = None if request.is_main_frame else (request.page_url or request.frame_url)
        inp = request_input(request.url, request.resource_type, page_url)
        if inp is None:  # data:, blob:, about:, chrome-extension: …
            return ALLOW
        engine = self._holder.engine
        decision = engine.decide(inp)
        block = decision.action is Action.BLOCK and not request.is_main_frame
        return InterceptDecision(
            action=InterceptAction.BLOCK if block else InterceptAction.ALLOW,
            rule_id=decision.rule_id,
            reason=decision.reason,
            classification=decision.classification.value,
            ruleset=decision.ruleset,
        )
