"""Durable product feedback captured from dogfooding Conversations."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from pydantic import UUID7, PositiveInt
from snekql.sqlite import CurrentTimestamp, Database, Fetched, insert, select, update

from tether.events import EventPublisher, InvalidateEvent, NullEventPublisher
from tether.product_observation_errors import (
    InvalidProductObservationError,
    ProductObservationConflictError,
    ProductObservationNotFoundError,
)
from tether.product_observation_store import ProductObservation


def product_observation_reference(
    observation_id: UUID, version: PositiveInt
) -> ProductObservation[Fetched]:
    """Build the identity and observed version required by a mutation."""
    return cast(
        "ProductObservation[Fetched]",
        ProductObservation.construct(
            id=observation_id,
            version=version,
            wording="",
            interpretation="",
            conversation_id=observation_id,
            message_id=observation_id,
            status="open",
        ),
    )


class ProductObservationService:
    """Record and manage explicit product feedback.

    ```python
    service = ProductObservationService(database)
    observation = await service.record(
        wording="Log that as feedback",
        interpretation="Feedback capture should preserve prior context",
        conversation_id=conversation_id,
        message_id=message_id,
    )
    assert observation.status == "open"
    ```
    """

    def __init__(
        self,
        database: Database,
        event_publisher: EventPublisher | None = None,
    ) -> None:
        self.database: Database = database
        self.event_publisher: EventPublisher = event_publisher or NullEventPublisher()

    async def record(
        self,
        *,
        wording: str,
        interpretation: str,
        conversation_id: UUID7,
        message_id: UUID7,
    ) -> ProductObservation[Fetched]:
        """Record one explicit observation with exact Message provenance."""
        normalized_interpretation = interpretation.strip()
        if not wording.strip() or not normalized_interpretation:
            message = "product observation text must not be blank"
            raise InvalidProductObservationError(message)
        async with self.database.transaction(mode="immediate") as transaction:
            observation = await transaction.execute(
                insert(
                    ProductObservation(
                        conversation_id=conversation_id,
                        interpretation=normalized_interpretation,
                        message_id=message_id,
                        status="open",
                        wording=wording,
                    )
                ).returning()
            )
        await self.event_publisher.publish(
            InvalidateEvent(keys=["product-observations"])
        )
        return observation

    async def list_open(self) -> list[ProductObservation[Fetched]]:
        """List unresolved observations newest first."""
        async with self.database.transaction() as transaction:
            return await transaction.fetch_all(
                select(ProductObservation)
                .where(ProductObservation.status.eq("open"))
                .order_by(ProductObservation.created_at.desc())
            )

    async def resolve(
        self, observation: ProductObservation[Fetched]
    ) -> ProductObservation[Fetched]:
        """Resolve an observation at its observed version."""
        async with self.database.transaction(mode="immediate") as transaction:
            matched = await transaction.execute(
                update(ProductObservation)
                .set(ProductObservation.status.to("resolved"))
                .set(ProductObservation.resolved_at.to(CurrentTimestamp))
                .set(ProductObservation.updated_at.to(CurrentTimestamp))
                .set(ProductObservation.version.to(observation.version + 1))
                .where(ProductObservation.id.eq(observation.id))
                .where(ProductObservation.version.eq(observation.version))
            )
            current = await transaction.fetch_one_or_none(
                select(ProductObservation).where(
                    ProductObservation.id.eq(observation.id)
                )
            )
            if current is None:
                raise ProductObservationNotFoundError(str(observation.id))
            if matched == 0:
                message = (
                    f"Product observation {observation.id} changed from version "
                    f"{observation.version} to {current.version}"
                )
                raise ProductObservationConflictError(message)
        await self.event_publisher.publish(
            InvalidateEvent(keys=["product-observations"])
        )
        return current
