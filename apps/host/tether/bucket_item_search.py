"""Deterministic bounded Bucket item search over canonical SQLite state."""

from opentelemetry.trace import Tracer
from pydantic import PositiveInt
from snekql.sqlite import Database, Fetched, select

from tether.bucket_item_store import BucketItem, bucket_item_index_text
from tether.structured_logging import Logger


class EmptyBucketSearchQueryError(Exception):
    """Raised when Bucket item search receives a blank query."""


def _matches(item: BucketItem[Fetched], terms: tuple[str, ...]) -> bool:
    """Match every normalized term against the item's deterministic text projection."""
    projected = "\n".join(
        (bucket_item_index_text(item), item.intent_context, item.item_type)
    ).casefold()
    return all(term in projected for term in terms)


class BucketItemSearchService:
    """Search Bucket items without an embedding or model dependency."""

    def __init__(self, database: Database, tracer: Tracer) -> None:
        self.database: Database = database
        self.tracer: Tracer = tracer

    async def search(
        self,
        query: str,
        limit: PositiveInt = 50,
        *,
        logger: Logger,
    ) -> list[BucketItem[Fetched]]:
        """Return at most 50 active items matching all case-insensitive terms."""
        terms = tuple(part.casefold() for part in query.split() if part)
        if not terms:
            message = "keyword Search requires a non-empty query"
            raise EmptyBucketSearchQueryError(message)
        bounded_limit = min(int(limit), 50)
        logger.debug("Searching Bucket items", limit=bounded_limit)
        with self.tracer.start_as_current_span(
            "BucketItemSearchService.search",
            attributes={"bucket_item.search.limit": bounded_limit},
        ) as span:
            async with self.database.transaction() as transaction:
                active = await transaction.fetch_all(
                    select(BucketItem)
                    .where(
                        BucketItem.completed_at.is_null()
                        & BucketItem.deleted_at.is_null()
                    )
                    .order_by(BucketItem.created_at.desc())
                )
            matches = [item for item in active if _matches(item, terms)][:bounded_limit]
            span.set_attribute("bucket_item.search.result_count", len(matches))
            return matches


__all__ = ["BucketItemSearchService", "EmptyBucketSearchQueryError"]
