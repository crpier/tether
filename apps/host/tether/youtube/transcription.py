"""YouTube's association with source-independent Transcriptions."""

from tether.transcripts import TranscriptionKey, TranscriptionTarget
from tether.youtube.types import VideoId

_YOUTUBE_TRANSCRIPTION_PREFIX = "youtube:"


def youtube_transcription_target(video_id: VideoId | str) -> TranscriptionTarget:
    """Map one YouTube identity to its stable Transcription target."""
    return TranscriptionTarget(
        key=TranscriptionKey(f"{_YOUTUBE_TRANSCRIPTION_PREFIX}{video_id}"),
        locator=f"https://www.youtube.com/watch?v={video_id}",
    )
