"""P6 Gate D: in-process filter decisions per second (design §21).

Ruleset: built_in + V1 (reviewed import) + EasyList + EasyPrivacy, parsed
from the lists in git-ignored ``var/filter-lists/`` (downloaded by the
import command). Workload: every link and every sub-resource reference
(script, img, stylesheet, iframe, media) of the W691 HTML corpus
(``var/p5-corpus/capture-1``), each with its page as first party — real
request/link URLs, not synthetic ones. The engine has no decision cache.

    env/bin/python benchmarks/p6-filter/throughput.py [--seconds 10]
"""

from __future__ import annotations

import argparse
import gc
import json
import platform
import random
import resource
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "benchmarks" / "p5-extraction"))

import corpus  # noqa: E402

from crawler2.filtering import abp, builtin, v1import  # noqa: E402
from crawler2.filtering.engine import FilterEngine  # noqa: E402
from crawler2.filtering.inputs import PSL_VERSION, link_input, request_input  # noqa: E402
from crawler2.filtering.model import Policy, Rule, RuleSource  # noqa: E402

LISTS = ROOT / "var" / "filter-lists"
CAPTURE = ROOT / "var" / "p5-corpus" / "capture-1"
V1_FILE = ROOT / "tests" / "fixtures" / "filtering" / "v1_domain_blacklist.txt"
RESOURCES = (
    ("script[src]", "src", "script"),
    ("img[src]", "src", "image"),
    ("link[rel=stylesheet][href]", "href", "stylesheet"),
    ("iframe[src]", "src", "document"),
    ("video[src], audio[src], source[src]", "src", "media"),
)


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def load_rules() -> tuple[list[Rule], dict[str, object]]:
    rules = builtin.built_in_rules()
    v1_rules, _ = v1import.parse_blacklist(V1_FILE.read_text(), v1import.Manifest.load())
    rules += v1_rules
    info: dict[str, object] = {
        "built_in": len(builtin.built_in_rules()),
        "v1_blacklist": len(v1_rules),
    }
    for source in (RuleSource.EASYLIST, RuleSource.EASYPRIVACY):
        text = (LISTS / f"{source.value}.txt").read_text()
        parsed, report = abp.parse_list(text, source)
        rules += parsed
        info[source.value] = {
            "rules": len(parsed),
            "version": report.header.get("version"),
            "unsupported": sum(report.unsupported.values()),
        }
    return rules, info


def workload() -> tuple[list[tuple[str, str, str]], dict[str, int]]:
    """(context, url, resource type or page) triples from the corpus, in page order."""
    items: list[tuple[str, str, str, str]] = []
    for page in corpus.load(CAPTURE):
        tree = LexborHTMLParser(page.body)
        base = page.final_url
        for node in tree.css("a[href]"):
            url = urljoin(base, node.attributes.get("href") or "")
            if url.startswith("http"):
                items.append(("link", url, "document", base))
        for selector, attr, kind in RESOURCES:
            for node in tree.css(selector):
                url = urljoin(base, node.attributes.get(attr) or "")
                if url.startswith("http"):
                    items.append(("request", url, kind, base))
    counts = Counter(i[0] for i in items)
    return items, {
        "pages": len(corpus.load(CAPTURE)),
        **counts,
        "distinct_urls": len({i[1] for i in items}),
    }


def build(items):
    out = []
    for context, url, kind, page in items:
        inp = (
            link_input(url, source_url=page)
            if context == "link"
            else request_input(url, kind, page)
        )
        if inp is not None:
            out.append(inp)
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--out", default=str(Path(__file__).parent / "results" / "throughput.json"))
    args = parser.parse_args()

    rss0 = rss_mb()
    t = time.perf_counter()
    rules, sources = load_rules()
    parse_s = time.perf_counter() - t
    gc.collect()
    t = time.perf_counter()
    engine = FilterEngine.compile(rules, Policy())
    compile_s = time.perf_counter() - t
    rss_engine = rss_mb()

    items, shape = workload()
    # Pages interleaved as concurrent workers would see them (reproducible, not security).
    random.Random(6).shuffle(items)  # noqa: S311
    # Cold: first time any URL/host is seen (PSL cache empty), input building included.
    t = time.perf_counter()
    inputs = build(items)
    decisions = [engine.decide(i) for i in inputs]
    cold_s = time.perf_counter() - t

    def rate(fn) -> tuple[float, int]:
        n = 0
        start = time.perf_counter()
        while time.perf_counter() - start < args.seconds:
            fn()
            n += len(inputs)
        return n / (time.perf_counter() - start), n

    warm_decide, n1 = rate(lambda: [engine.decide(i) for i in inputs])
    warm_e2e, _ = rate(lambda: [engine.decide(i) for i in build(items)])
    sample = inputs[: min(20_000, len(inputs))]
    lat = []
    for inp in sample:
        s = time.perf_counter_ns()
        engine.decide(inp)
        lat.append(time.perf_counter_ns() - s)
    lat.sort()

    result = {
        "gate": ">= 100000 decisions/s in-process (warm, no decision cache)",
        "gate_metric": "warm_decisions_per_s",
        "passed": warm_decide >= 100_000,
        "python": platform.python_version(),
        "machine": platform.processor() or platform.machine(),
        "psl": PSL_VERSION,
        "ruleset": {
            "id": engine.ruleset_id,
            "compiled_rules": engine.rule_count,
            "kinds": engine.counts,
            "untokenized_patterns": engine.untokenized,
            "sources": sources,
        },
        "load": {
            "parse_lists_s": round(parse_s, 2),
            "compile_s": round(compile_s, 2),
            "rss_before_mb": round(rss0, 1),
            "rss_after_compile_mb": round(rss_engine, 1),
        },
        "workload": shape | {"inputs": len(inputs)},
        "decisions": dict(Counter(f"{d.classification.value}:{d.action.value}" for d in decisions)),
        "cold_first_pass_per_s": round(len(inputs) / cold_s),
        "warm_decisions_per_s": round(warm_decide),
        "warm_url_to_decision_per_s": round(warm_e2e),
        "warm_decisions_counted": n1,
        "latency_us": {
            "p50": round(lat[len(lat) // 2] / 1000, 2),
            "p99": round(lat[int(len(lat) * 0.99)] / 1000, 2),
            "max": round(lat[-1] / 1000, 2),
        },
        "rss_peak_mb": round(rss_mb(), 1),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
