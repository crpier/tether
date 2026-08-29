"""Ordered application schema composition across host domains."""

from snekql.sqlite import Database

from tether.artifact_store import create_artifact_schema
from tether.attachment_store import create_attachment_schema
from tether.bucket_item_store import create_bucket_item_schema
from tether.conversation_store import create_conversation_schema
from tether.dreaming_store import create_dreaming_schema
from tether.ebook_stats_store import create_ebook_stats_schema
from tether.email_evidence_store import create_email_evidence_schema
from tether.gmail import create_gmail_schema
from tether.health_connect import create_health_moment_schema, create_health_plan_schema
from tether.kosync_store import create_kosync_schema
from tether.ledger_store import create_ledger_schema
from tether.memory_store import create_memory_schema
from tether.notification_store import create_notification_schema
from tether.panel_store import create_panel_schema
from tether.product_observation_store import create_product_observation_schema
from tether.push_store import create_push_schema
from tether.readwise_store import create_readwise_schema
from tether.recall_store import create_recall_schema
from tether.search_projection.metadata import create_search_meta_schema
from tether.todo_store import create_todo_schema
from tether.trigger_store import create_trigger_schema
from tether.youtube import create_youtube_schema


async def create_host_schema(database: Database) -> None:
    """Apply every request-serving domain's ordered migrations."""
    await create_memory_schema(database)
    await create_bucket_item_schema(database)
    await create_conversation_schema(database)
    await create_attachment_schema(database)
    await create_dreaming_schema(database)
    await create_health_moment_schema(database)
    await create_health_plan_schema(database)
    await create_youtube_schema(database)
    await create_trigger_schema(database)
    await create_push_schema(database)
    await create_recall_schema(database)
    await create_search_meta_schema(database)
    await create_notification_schema(database)
    await create_product_observation_schema(database)
    await create_artifact_schema(database)
    await create_panel_schema(database)
    await create_todo_schema(database)
    await create_readwise_schema(database)
    await create_kosync_schema(database)
    await create_ebook_stats_schema(database)
    await create_gmail_schema(database)
    await create_email_evidence_schema(database)
    await create_ledger_schema(database)
