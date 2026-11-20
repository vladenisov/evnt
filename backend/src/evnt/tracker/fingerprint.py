"""``extra.event_fingerprint``: a hash of an event as its tracker sent it.

A tracker resends a batch it got no success answer for, so one event can be
stored more than once. The fingerprint lets those copies be found later: it
hashes the event's parameters as received, leaving out the ones that change
between sends, so every send of one event gives the same value.

Left out, as in Snowplow's own ``event_fingerprint``: ``eid`` (some trackers
and bots resend the same event under a new id), ``stm`` (the send time),
``nuid`` and ``cv`` (set by a collector, not the tracker).
"""

from hashlib import blake2b
from operator import itemgetter
from typing import Any, Final

import orjson

from evnt.tracker.models import PayloadElementModel

EXCLUDED_PARAMS: Final[tuple[str, ...]] = ("eid", "stm", "nuid", "cv")
FINGERPRINT_BYTES: Final[int] = 16

_first = itemgetter(0)


def event_fingerprint(raw: Any) -> str:
    """Hex blake2b digest of an event's parameters, without the send-time ones.

    ``raw`` is the event as received (a JSON object of a POST batch, or a
    pixel's query parameters). An already validated element, which only
    internal callers pass, is hashed from the fields that were set on it.
    """

    if isinstance(raw, PayloadElementModel):
        params = raw.model_dump(mode="json", include=raw.model_fields_set)
    else:
        params = dict(raw)
    for name in EXCLUDED_PARAMS:
        params.pop(name, None)
    try:
        canonical = orjson.dumps(params, option=orjson.OPT_SORT_KEYS)
    except orjson.JSONEncodeError:
        # An integer beyond 64 bits (the request body is parsed by the
        # standard library, which keeps them) or a non-string key; still
        # deterministic, just slower.
        canonical = repr(sorted(params.items(), key=_first)).encode()
    return blake2b(canonical, digest_size=FINGERPRINT_BYTES).hexdigest()
