"""Ordered application schema composition across host domains."""

from snekql.sqlite import Database

from tether.artifacts import create_artifact_schema
from tether.bucket_item_store import create_bucket_item_schema
from tether.conversations import create_conversation_schema
from tether.ebook_stats import create_ebook_stats_schema
from tether.gmail_store import create_gmail_schema
from tether.kosync import create_kosync_schema
from tether.memory_store import create_memory_schema
from tether.notifications import create_notification_schema
from tether.panels import create_panel_schema
from tether.proposal_store import create_proposal_schema
from tether.push import create_push_schema
from tether.readwise_store import create_readwise_schema
from tether.recall_store import create_recall_schema
from tether.search_meta import create_search_meta_schema
from tether.todos import create_todo_schema
from tether.triggers import create_trigger_schema
from tether.youtube_store import create_youtube_schema


async def create_host_schema(database: Database) -> None:
    """Apply every request-serving domain's ordered migrations."""
    await create_memory_schema(database)
    await create_bucket_item_schema(database)
    await create_conversation_schema(database)
    await create_youtube_schema(database)
    await create_trigger_schema(database)
    await create_push_schema(database)
    await create_recall_schema(database)
    await create_search_meta_schema(database)
    await create_notification_schema(database)
    await create_proposal_schema(database)
    await create_artifact_schema(database)
    await create_panel_schema(database)
    await create_todo_schema(database)
    await create_readwise_schema(database)
    await create_kosync_schema(database)
    await create_ebook_stats_schema(database)
    await create_gmail_schema(database)
