"""Context entities kept in ``extra.contexts``.

Every entity that no handler in ``evnt.tracker.payload.CONTEXT_HANDLERS``
turns into columns, and every repeat of a handled schema on the same event
(the handlers merge or overwrite repeats), is kept here instead of being
dropped:

    extra.contexts = {"vendor/name": [{"version": "1-0-0", "data": "<JSON>"}, ...]}

Entities of one schema keep their arrival order.

``data`` is the entity's JSON text, not a nested object. ``extra`` is a
ClickHouse ``JSON`` column, and arbitrary tracker JSON nested in it can fail
the whole insert: ``{"a.b": 1, "a": {"b": 2}}`` is a duplicate path that
ClickHouse rejects (INCORRECT_DATA), and with ``wait_for_async_insert=0`` that
loses the request's rows silently. Nested objects would also give every key of
every unknown schema its own subcolumn, against the column's dynamic path
limit shared with the rest of ``extra``. As text, each schema costs one path
holding ``version`` and ``data`` strings, whatever the tracker sends; it is
read back with ``JSONExtract*`` (see ``website/docs/integrate/trackers.md``).
"""

from typing import Any, Final

import orjson

# The trackers cap a POST at 40 000 bytes by default (``maxPostBytes`` in the
# JavaScript tracker, ``byteLimitPost`` on iOS and Android), so no real event
# carries more than that in entities. 64 KiB stays above it, and 256 entities
# is far above what a product list view (dozens of ecommerce ``product``
# entities) attaches, so neither limit is reached by real tracking.
MAX_STORED_ENTITIES: Final[int] = 256
MAX_STORED_ENTITY_BYTES: Final[int] = 64 * 1024


class StoredEntities:
    """The entities one event keeps in ``extra.contexts``, within the limits."""

    __slots__ = ("by_schema", "count", "dropped", "size")

    def __init__(self) -> None:
        self.by_schema: dict[str, list[dict[str, str]]] = {}
        self.count = 0
        self.size = 0
        self.dropped = 0

    def add(self, key: str, version: str, data: Any) -> bool:
        """Keep one entity; ``False`` if it is over the limits and was not kept.

        An entity that does not fit is skipped and counted, and smaller ones
        after it are still kept, so one oversized entity costs only itself.
        """

        if self.count >= MAX_STORED_ENTITIES:
            self.dropped += 1
            return False
        # Values come from ``orjson.loads``, which always serializes back; a
        # caller-built value that does not is kept as its ``repr``.
        try:
            encoded = orjson.dumps(data)
        except orjson.JSONEncodeError:
            encoded = orjson.dumps(repr(data))
        size = len(encoded)
        if self.size + size > MAX_STORED_ENTITY_BYTES:
            self.dropped += 1
            return False
        self.count += 1
        self.size += size
        entry = {"version": version, "data": encoded.decode()}
        entries = self.by_schema.get(key)
        if entries is None:
            self.by_schema[key] = [entry]
        else:
            entries.append(entry)
        return True
