"""Prometheus counters for what the collector does with tracker input.

They live in the default registry, so ``/metrics`` exposes them whenever
``EVNT_PROMETHEUS__ENABLED=true``; with it off they still count, unread.
``prometheus_client`` comes with ``prometheus-fastapi-instrumentator``, a
required dependency.
"""

from typing import Final

from prometheus_client import Counter

EVENTS_DROPPED: Final = Counter(
    "evnt_events_dropped_total",
    "Tracker events that could not be built into a row and were dropped; "
    "the rest of their batch was stored.",
    ("reason",),
)

GEO_VALUES_REJECTED: Final = Counter(
    "evnt_geo_header_values_rejected_total",
    "Cloudflare visitor location header values that failed validation and were "
    "left out of extra.collector.geo; the request was processed as usual.",
    ("field",),
)
