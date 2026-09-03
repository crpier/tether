"""Ordered application schema composition across host domains."""

from typing import cast

from snekql.sqlite import Database

from tether.artifact_store import create_artifact_schema
from tether.attachment_store import create_attachment_schema
from tether.bucket_item_store import create_bucket_item_schema
from tether.conversation_store import (
    backfill_historical_turns,
    canonicalize_conversation_schema,
    conversation_migrations,
)
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
from tether.transcripts import create_transcript_schema
from tether.trigger_store import create_trigger_schema
from tether.youtube import (
    create_youtube_schema,
    migrate_legacy_youtube_transcripts,
    remove_legacy_youtube_transcript_storage,
)

_DEPLOYED_LEGACY_MIGRATION_NAMES = (
    # Oldest observed history. Every later deployed history contains this set.
    "001_memories",
    "002_memory_embedding",
    "003_memory_embedded_version",
    "004_memory_facets",
    "027_drop_legacy_memory",
    "002_create_bucket_item",
    "002_create_index_ix_bucket_item_item_type_dedup_key",
    "003_create_conversation",
    "003_create_message",
    "001_create_dream_conversation_cursor",
    "002_create_dream_run",
    "003_create_dreaming_mutation",
    "004_create_index_ix_dreaming_mutation_run_id_tool_call_id",
    "005_create_dreaming_workspace_file",
    "006_dreaming_mutation_before_content",
    "007_dreaming_mutation_after_content",
    "008_create_health_dream_run",
    "004_create_ingested_video",
    "004_create_index_ux_ingested_video_video_id",
    "004_create_index_ix_ingested_video_topic",
    "005_ingested_video_channel_id",
    "005_ingested_video_liked_at",
    "005_ingested_video_video_published_at",
    "005_ingested_video_duration_seconds",
    "005_ingested_video_category_id",
    "005_ingested_video_default_language",
    "005_ingested_video_default_audio_language",
    "005_ingested_video_caption_available",
    "005_ingested_video_privacy_status",
    "005_ingested_video_licensed_content",
    "005_ingested_video_made_for_kids",
    "005_ingested_video_live_broadcast_content",
    "005_ingested_video_definition",
    "005_ingested_video_dimension",
    "005_ingested_video_statistics_view_count",
    "005_ingested_video_statistics_like_count",
    "005_ingested_video_statistics_comment_count",
    "005_ingested_video_statistics_fetched_at",
    "005_ingested_video_topic_categories_json",
    "005_ingested_video_tags_json",
    "005_ingested_video_thumbnails_json",
    "006_create_you_tube_quota_daily",
    "007_create_you_tube_sync_state",
    "008_create_you_tube_transcript_state",
    "009_ingested_video_transcript_segments_json",
    "009_ingested_video_transcript_source",
    "010_normalize_transcript_status",
    "011_normalize_transcript_state_defaults",
    "012_remove_duplicate_available_transcript_state",
    "005_create_scheduled_trigger",
    "005_create_index_ix_scheduled_trigger_status_next_fire_at",
    "006_create_push_subscription",
    "006_create_index_ux_push_subscription_endpoint",
    "007_create_study_item",
    "007_create_index_ux_study_item_source_video_id",
    "007_create_index_ix_study_item_state",
    "007_create_recall_prompt",
    "007_create_index_ix_recall_prompt_study_item_id_due_at",
    "007_create_recall_answer",
    "007_create_index_ix_recall_answer_prompt_id",
    "010_recall_prompt_reference_answer",
    "010_recall_prompt_rubric",
    "010_recall_answer_answer_text",
    "017_recall_add_distilled_learnings",
    "018_recall_drop_memory_id",
    "008_search_meta",
    "009_create_notification",
    "009_create_index_ix_notification_dismissed_at_created_at",
    "030_create_proposal",
    "030_create_index_ix_proposal_state_created_at",
    "030_create_proposal_action",
    "030_create_index_ix_proposal_action_proposal_id_seq",
    "030_create_autonomy_grant",
    "031_proposal_action_display",
    "011_create_artifact",
    "011_create_index_ix_artifact_artifact_id_version",
    "011_create_artifact_event",
    "011_create_index_ix_artifact_event_artifact_id",
    "012_create_synthetic_panel",
    "012_create_index_ix_synthetic_panel_deleted_at_position",
    "013_create_todo",
    "013_create_index_ix_todo_status",
    "013_create_todo_memory",
    "013_create_index_ix_todo_memory_todo_id",
    "026_drop_todo_memory",
    "001_create_readwise_highlight",
    "002_create_readwise_sync_state",
    "019_drop_readwise_memory_mapping",
    "020_create_readwise_evidence",
    "029_reset_readwise_highlight_watermark",
    "001_create_ebook_progress_event",
    "002_index_ebook_progress_event_document_hash",
    "003_create_ebook_document",
    "028_rename_ebook_finished_at",
    "001_create_ebook_stat_book",
    "002_index_ebook_stat_book_source_book_id",
    "003_create_ebook_stat_page_event",
    "004_index_ebook_stat_page_event_book_start_time",
    "005_index_ebook_stat_page_event_natural_key",
    "006_create_ebook_stat_sync_state",
    "001_create_gmail_message",
    "002_create_gmail_sync_state",
    "021_gmail_from_header",
    "022_gmail_subject",
    "023_gmail_body_text",
    "024_gmail_verdict_reason",
    "025_gmail_drop_memory_id",
    # Migrations observed in the next deployed history.
    "034_conversation_archived_at",
    "034_conversation_display_name",
    "034_conversation_kind",
    "034_conversation_last_read_seq",
    "034_conversation_scope_brief",
    "034_conversation_scope_revision",
    "034_conversation_status",
    "034_conversation_migrate_existing",
    "034_conversation_mark_oldest_main",
    "034_conversation_create_current",
    "034_conversation_copy_current",
    "034_conversation_drop_legacy",
    "034_conversation_rename_current",
    "034_conversation_single_main_insert",
    "034_conversation_single_main_update",
    "035_conversation_runtime_scope_revision",
    "035_create_conversation_turn",
    "035_message_turn_id",
    "035_message_turn_message_seq",
    "035_turn_interactive_request_unique",
    "035_turn_scheduled_occurrence_unique",
    "035_turn_message_sequence_unique",
    "037_turn_cancel_requested_at",
    "037_turn_execution_lease_id",
    "037_turn_model_display_name_snapshot",
    "037_turn_model_id_snapshot",
    "037_turn_model_provider_snapshot",
    "037_turn_model_thinking_level_snapshot",
    "037_turn_seq",
    "037_turn_seq_backfill",
    "037_turn_sequence_unique",
    "037_turn_initiating_message_unique",
    "009_create_dream_maintenance_progress",
    "039_create_health_moment",
    "039_health_moment_source_unique",
    "040_create_health_plan",
    "040_health_plan_status_created_index",
    "040_create_planned_exercise_occurrence",
    "040_planned_exercise_occurrence_source_unique",
    "040_planned_exercise_occurrence_plan_grace_index",
    "033_scheduled_trigger_model_profile",
    "036_scheduled_trigger_target_conversation",
    "036_create_scheduled_occurrence",
    "036_scheduled_occurrence_trigger_fire_unique",
    "036_scheduled_occurrence_status_push_index",
    "038_occurrence_dispatch_attempts",
    "038_occurrence_model_display_name_snapshot",
    "038_occurrence_model_id_snapshot",
    "038_occurrence_model_provider_snapshot",
    "038_occurrence_model_thinking_level_snapshot",
    "038_occurrence_next_attempt_at",
    "038_occurrence_retry_index",
    "032_create_product_observation",
    "032_create_index_ix_product_observation_status_created_at",
    # Migrations observed in the newest pre-0.6 history.
    "038_create_message_attachment",
    "038_attachment_message_index",
    "038_attachment_turn_index",
    "001_create_email_evidence_snapshot",
    "002_email_evidence_snapshot_source_unique",
    "003_create_email_evidence_promotion",
    "004_email_evidence_promotion_source_unique",
    # Migrations present in the live pre-0.6 history deployed on 2026-08-29.
    "041_create_ledger_proposal",
    "041_create_index_ix_ledger_proposal_status_created_at",
    "041_create_index_ix_ledger_proposal_ledger_id_created_at",
    "041_create_ledger",
    "041_create_ledger_revision",
    "041_create_unique_index_ix_ledger_revision_ledger_revision",
    "041_create_unique_index_ix_ledger_revision_proposal_id",
    "041_create_ledger_entry",
    "041_create_index_ix_ledger_entry_ledger_recorded_at",
    "041_create_unique_index_ix_ledger_entry_dedupe_key",
    "041_create_unique_index_ix_ledger_entry_supersedes",
)

_RETIRED_MIGRATION_NAMES = frozenset(
    {
        "030_create_proposal",
        "030_create_index_ix_proposal_state_created_at",
        "030_create_proposal_action",
        "030_create_index_ix_proposal_action_proposal_id_seq",
        "030_create_autonomy_grant",
        "031_proposal_action_display",
    }
)


class HostMigrationNameCollisionError(Exception):
    """Raised when two host domains claim the same migration identity."""


class _MigrationCollector:
    """Collect domain declarations before touching the shared main database."""

    def __init__(self) -> None:
        self.migrations: dict[str, str] = {}

    async def migrate(self, migrations: dict[str, str]) -> None:
        """Append one domain chain while preserving global declaration order."""
        for name, sql in migrations.items():
            if name in self.migrations:
                message = f"duplicate host migration name: {name}"
                raise HostMigrationNameCollisionError(message)
            self.migrations[name] = sql


class HostMigrationHistoryError(Exception):
    """Raised when a deployed migration identity has no current declaration."""


async def host_migrations() -> dict[str, str]:
    """Collect the main database's complete ordered migration declaration."""
    collector = _MigrationCollector()
    target = cast("Database", collector)
    await create_memory_schema(target)
    await create_bucket_item_schema(target)
    await target.migrate(conversation_migrations())
    await create_attachment_schema(target)
    await create_dreaming_schema(target)
    await create_health_moment_schema(target)
    await create_health_plan_schema(target)
    await create_youtube_schema(target)
    await create_transcript_schema(target)
    await migrate_legacy_youtube_transcripts(target)
    await create_trigger_schema(target)
    await create_push_schema(target)
    await create_recall_schema(target)
    await create_search_meta_schema(target)
    await create_notification_schema(target)
    await create_product_observation_schema(target)
    await create_artifact_schema(target)
    await create_panel_schema(target)
    await create_todo_schema(target)
    await create_readwise_schema(target)
    await create_kosync_schema(target)
    await create_ebook_stats_schema(target)
    await create_gmail_schema(target)
    await create_email_evidence_schema(target)
    await create_ledger_schema(target)

    declarations = dict(collector.migrations)
    for name in _RETIRED_MIGRATION_NAMES:
        if name in declarations:
            message = f"retired host migration is active again: {name}"
            raise HostMigrationNameCollisionError(message)
        declarations[name] = "SELECT 1"

    ordered: dict[str, str] = {}
    for name in _DEPLOYED_LEGACY_MIGRATION_NAMES:
        try:
            ordered[name] = declarations.pop(name)
        except KeyError:
            message = f"deployed host migration has no declaration: {name}"
            raise HostMigrationHistoryError(message) from None
    ordered.update(declarations)
    return ordered


async def create_host_schema(database: Database) -> None:
    """Apply the main database's one complete ordered migration chain."""
    await database.migrate(await host_migrations(), adopt_legacy=True)
    await remove_legacy_youtube_transcript_storage(database)
    await canonicalize_conversation_schema(database)
    await backfill_historical_turns(database)
