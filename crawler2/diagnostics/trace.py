"""Follow one unit of work through the retained event streams (P14, TC-055).

Every event envelope carries ``correlation_id`` (shared by a causal chain) and ``causation_id``
(the event that directly caused it). ``crawler2-trace NEEDLE`` finds the events that mention
NEEDLE anywhere (a URL id, observation id, media id, content id, match id, evidence id, a
correlation id, an event id), then widens to every event sharing those correlation ids, and
prints the chain in time order across event types.

Limits, stated rather than hidden:

* streams are trimmed (ADR-022): an old chain is partly gone from Redis; the durable copy is the
  Scylla outbox and the entity tables (``crawler2-media show``, ``crawler2-evidence show``);
* a correlation id is minted per recorded fetch attempt and propagated by extraction and the
  media registry. The fingerprinter and the evidence finalizer do not propagate it yet, so the
  chain continues by shared ids (content id -> representation id -> match id -> evidence id)
  rather than by correlation id; searching one of those ids finds that stage directly.

    crawler2-trace NEEDLE [--max-scan 200000] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from typing import Any

from crawler2.core.configuration import Settings
from crawler2.diagnostics.exporter import EVENT_TYPES
from crawler2.storage.events.consumer import connect_stream_client
from crawler2.storage.events.publisher import stream_name

_BATCH = 1000


@dataclass(frozen=True)
class Hop:
    entry_id: str
    event_type: str
    occurred_at: str
    event_id: str
    correlation_id: str | None
    causation_id: str | None
    producer: str
    matched_directly: bool


def _scan(
    client: Any, streams: dict[str, str], *, max_scan: int
) -> tuple[list[tuple[str, str, dict[str, Any], str]], dict[str, int]]:
    """(event_type, entry_id, envelope, raw) for up to ``max_scan`` entries per stream."""
    events: list[tuple[str, str, dict[str, Any], str]] = []
    scanned: dict[str, int] = {}
    for etype, key in streams.items():
        cursor, seen = "-", 0
        while seen < max_scan:
            try:
                batch = client.xrange(key, min=cursor, count=min(_BATCH, max_scan - seen))
            except Exception:  # noqa: BLE001 -- a stream that does not exist is simply empty
                break
            if cursor != "-" and batch and batch[0][0] == cursor:
                batch = batch[1:]
            if not batch:
                break
            for entry_id, fields in batch:
                raw = fields.get(b"envelope") or fields.get("envelope")
                text = raw.decode() if isinstance(raw, bytes) else str(raw)
                try:
                    env = json.loads(text)
                except ValueError:
                    continue
                events.append(
                    (etype, entry_id if isinstance(entry_id, str) else entry_id.decode(), env, text)
                )
            seen += len(batch)
            cursor = batch[-1][0] if isinstance(batch[-1][0], str) else batch[-1][0].decode()
        scanned[etype] = seen
    return events, scanned


def trace(
    client: Any, streams: dict[str, str], needle: str, *, max_scan: int = 200_000
) -> tuple[list[Hop], dict[str, int]]:
    events, scanned = _scan(client, streams, max_scan=max_scan)
    direct = {i for i, (_, _, _, raw) in enumerate(events) if needle in raw}
    correlations = {
        events[i][2].get("correlation_id") for i in direct if events[i][2].get("correlation_id")
    }
    chosen = set(direct)
    for i, (_, _, env, _) in enumerate(events):
        if env.get("correlation_id") in correlations:
            chosen.add(i)
    hops = [
        Hop(
            entry_id=events[i][1],
            event_type=events[i][0],
            occurred_at=str(events[i][2].get("occurred_at", "")),
            event_id=str(events[i][2].get("event_id", "")),
            correlation_id=events[i][2].get("correlation_id"),
            causation_id=events[i][2].get("causation_id"),
            producer=json.dumps(events[i][2].get("producer", {}), sort_keys=True),
            matched_directly=i in direct,
        )
        for i in chosen
    ]
    hops.sort(key=lambda h: (h.occurred_at, h.entry_id))
    return hops, scanned


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crawler2-trace", description=__doc__)
    parser.add_argument("needle", help="an id (or any string) to look for in retained events")
    parser.add_argument("--max-scan", type=int, default=200_000, help="entries read per stream")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if len(args.needle) < 8:
        print("refusing: the needle is too short to identify anything", file=sys.stderr)
        return 2
    settings = Settings()
    client = connect_stream_client(settings.redis)
    streams = {t: stream_name(settings.events, t, major) for t, major in EVENT_TYPES}
    hops, scanned = trace(client, streams, args.needle, max_scan=args.max_scan)
    if args.json:
        print(json.dumps({"hops": [asdict(h) for h in hops], "scanned": scanned}, indent=2))
    else:
        for h in hops:
            mark = "*" if h.matched_directly else " "
            print(
                f"{h.occurred_at} {mark} {h.event_type:<26} "
                f"event={h.event_id} corr={h.correlation_id}"
            )
        print(
            f"-- {len(hops)} event(s); scanned {sum(scanned.values())} retained entries",
            file=sys.stderr,
        )
    return 0 if hops else 1


if __name__ == "__main__":
    sys.exit(main())
