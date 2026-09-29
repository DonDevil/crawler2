"""V1 parse/extraction CPU per page (P5 exit gate). Runs in V1's own venv, read-only.

    cd ~/anti_piracy/crawler && PYTHONDONTWRITEBYTECODE=1 env/bin/python \
        ~/anti_piracy/crawler2/benchmarks/p5-extraction/v1_parse.py CAPTURE --out FILE

Times ``HTMLLinkExtractor().extract_content(html, url)`` — the call every
V1 crawler makes per page (links + media; two BeautifulSoup parses) — with
``time.process_time()`` per page. V1 as deployed: blacklist enabled, but
pointed at a scratch *copy* because ``clean_url`` appends to it (audit V-3).
Loguru sinks are removed (V1 runs at INFO; no log I/O is charged to V1).
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path.cwd()))

import bs4
import lxml.etree
from corpus import load, summarize
from loguru import logger
from parsers.html_link_extractor import HTMLLinkExtractor
from utils.url_utils import URLUtils


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("capture", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--passes", type=int, default=5)
    args = parser.parse_args()
    logger.remove()
    scratch = Path(tempfile.mkdtemp(prefix="p5-v1-"))
    blacklist = scratch / "domain_blacklist.txt"
    shutil.copy(Path("datasets/domain_blacklist.txt"), blacklist)
    URLUtils.set_blacklist_path(str(blacklist))
    URLUtils.set_blacklist_enabled(True)

    pages = load(args.capture)
    extractor = HTMLLinkExtractor()
    html = [p.body.decode("utf-8", errors="replace") for p in pages]
    counts = []
    for text, page in zip(html, pages, strict=True):  # warm-up pass
        content = extractor.extract_content(text, page.final_url)
        counts.append((len(content["links"]), len(content["media_links"])))
    per_page = [0.0] * len(pages)
    total_start = time.process_time()
    for _ in range(args.passes):
        for i, (text, page) in enumerate(zip(html, pages, strict=True)):
            start = time.process_time()
            extractor.extract_content(text, page.final_url)
            per_page[i] += time.process_time() - start
    total = time.process_time() - total_start
    samples = [t / args.passes * 1e3 for t in per_page]
    result = {
        "system": "v1",
        "call": "HTMLLinkExtractor.extract_content (links + media)",
        "passes": args.passes,
        "aggregate_ms_per_page": round(total / args.passes / len(pages) * 1e3, 3),
        **summarize(samples),
        "links_total": sum(c[0] for c in counts),
        "media_total": sum(c[1] for c in counts),
        "versions": {
            "python": platform.python_version(),
            "bs4": bs4.__version__,
            "lxml": ".".join(map(str, lxml.etree.LXML_VERSION)),
        },
        "per_page_ms": {p.sha256: round(s, 3) for p, s in zip(pages, samples, strict=True)},
    }
    shutil.rmtree(scratch)
    args.out.write_text(json.dumps(result, indent=1))
    print(json.dumps({k: v for k, v in result.items() if k != "per_page_ms"}))


if __name__ == "__main__":
    main()
