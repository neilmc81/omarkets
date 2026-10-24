"""News and geopolitical source adapters (Phase 5).

Vendor/feed-specific code is isolated here behind :class:`NewsSource`. The
pipeline above depends only on the interface and the typed
:class:`~core.domain.news.SourceItem` schema.
"""

from core.providers.news.base import (
    Clock,
    HttpNewsSource,
    NewsSource,
    NewsSourceError,
    content_hash,
    utc_now,
)
from core.providers.news.catalog import (
    DEFAULT_ENABLED_SOURCES,
    SOURCE_DEFINITIONS,
    SourceDefinition,
    build_source,
)

__all__ = [
    "Clock",
    "DEFAULT_ENABLED_SOURCES",
    "HttpNewsSource",
    "NewsSource",
    "NewsSourceError",
    "SOURCE_DEFINITIONS",
    "SourceDefinition",
    "build_source",
    "content_hash",
    "utc_now",
]
