"""Behaviour tests for durable Product observations."""

from collections.abc import AsyncGenerator
from uuid import uuid7

from snekql.sqlite import Config, Database
from snektest import assert_eq, assert_is_not_none, fixture, load_fixture, test

from tether.product_observation_store import create_product_observation_schema
from tether.product_observations import (
    ProductObservationService,
    product_observation_reference,
)


@fixture
async def observation_service() -> AsyncGenerator[ProductObservationService]:
    """A Product-observation service over a fresh database."""
    database = await Database.initialize(backend=Config(database=":memory:"))
    await create_product_observation_schema(database)
    yield ProductObservationService(database)
    await database.close()


@test()
async def recording_feedback_preserves_the_source_and_opens_the_observation() -> None:
    """Recording explicit feedback retains exact conversational provenance."""
    service = await load_fixture(observation_service())
    conversation_id = uuid7()
    message_id = uuid7()

    observation = await service.record(
        wording="You should have reminded me about that workout.",
        interpretation="Tether should resurface same-day exercise intentions.",
        conversation_id=conversation_id,
        message_id=message_id,
    )

    assert_eq(observation.wording, "You should have reminded me about that workout.")
    assert_eq(
        observation.interpretation,
        "Tether should resurface same-day exercise intentions.",
    )
    assert_eq(observation.conversation_id, conversation_id)
    assert_eq(observation.message_id, message_id)
    assert_eq(observation.status, "open")
    assert_eq(observation.version, 1)


@test()
async def resolving_an_observation_closes_it_at_the_observed_version() -> None:
    """Resolution is a versioned terminal lifecycle transition."""
    service = await load_fixture(observation_service())
    observation = await service.record(
        wording="This interaction could be better.",
        interpretation="Capture the interaction as product feedback.",
        conversation_id=uuid7(),
        message_id=uuid7(),
    )

    resolved = await service.resolve(
        product_observation_reference(observation.id, observation.version)
    )

    assert_eq(resolved.status, "resolved")
    assert_eq(resolved.version, 2)
    assert_is_not_none(resolved.resolved_at)


@test()
async def listing_open_observations_excludes_resolved_feedback() -> None:
    """The active list contains feedback still awaiting a product response."""
    service = await load_fixture(observation_service())
    resolved = await service.record(
        wording="Old feedback",
        interpretation="Already addressed",
        conversation_id=uuid7(),
        message_id=uuid7(),
    )
    open_observation = await service.record(
        wording="Current feedback",
        interpretation="Still needs attention",
        conversation_id=uuid7(),
        message_id=uuid7(),
    )
    _ = await service.resolve(
        product_observation_reference(resolved.id, resolved.version)
    )

    observations = await service.list_open()

    assert_eq([observation.id for observation in observations], [open_observation.id])
