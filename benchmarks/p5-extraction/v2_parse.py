"""V2 parse/extraction CPU per page (P5 exit gate), same corpus and method as ``v1_parse.py``.

    env/bin/python benchmarks/p5-extraction/v2_parse.py CAPTURE --out FILE

Times the full P5 pipeline ``extract(body, page=, content_type=)`` per page —
decoding, the one parse, links, media, metadata, script/text literals and
all six hashes — with ``time.process_time()``. V1's timed call does less
(links + media only, input already decoded), so the comparison is
conservative for V2. ``parse_ms`` (decode + parse only) is informational.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path

import selectolax
from antipiracy_contracts.models.web import UrlRef

from crawler2.extraction.extract import extract
from crawler2.extraction.model import Limits
from crawler2.extraction.parse import parse_page

sys.path.insert(0, str(Path(__file__).resolve().parent))
from corpus import load, summarize


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("capture", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--passes", type=int, default=5)
    args = parser.parse_args()
    pages = load(args.capture)
    refs = [UrlRef.of(p.final_url) for p in pages]
    limits = Limits()
    counts = []
    for page, ref in zip(pages, refs, strict=True):  # warm-up pass
        e = extract(page.body, page=ref, content_type=page.content_type)
        counts.append((len(e.links), len(e.media)))
    per_page = [0.0] * len(pages)
    total_start = time.process_time()
    for _ in range(args.passes):
        for i, (page, ref) in enumerate(zip(pages, refs, strict=True)):
            start = time.process_time()
            extract(page.body, page=ref, content_type=page.content_type)
            per_page[i] += time.process_time() - start
    total = time.process_time() - total_start
    parse_start = time.process_time()
    for page, ref in zip(pages, refs, strict=True):
        parse_page(
            page.body, page=ref, content_type=page.content_type, max_bytes=limits.max_parse_bytes
        )
    parse_total = time.process_time() - parse_start
    samples = [t / args.passes * 1e3 for t in per_page]
    result = {
        "system": "v2",
        "call": "crawler2.extraction.extract.extract (full P5 pipeline)",
        "passes": args.passes,
        "aggregate_ms_per_page": round(total / args.passes / len(pages) * 1e3, 3),
        **summarize(samples),
        "parse_only_ms_per_page": round(parse_total / len(pages) * 1e3, 3),
        "links_total": sum(c[0] for c in counts),
        "media_total": sum(c[1] for c in counts),
        "versions": {"python": platform.python_version(), "selectolax": selectolax.__version__},
        "per_page_ms": {p.sha256: round(s, 3) for p, s in zip(pages, samples, strict=True)},
    }
    args.out.write_text(json.dumps(result, indent=1))
    print(json.dumps({k: v for k, v in result.items() if k != "per_page_ms"}))


if __name__ == "__main__":
    main()
