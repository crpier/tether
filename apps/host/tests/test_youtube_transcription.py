"""YouTube association tests for source-independent Transcription targets."""

from snektest import assert_eq, test

from tether.youtube.transcription import youtube_transcription_target


@test()
def youtube_video_maps_to_a_generic_media_locator() -> None:
    """The YouTube adapter owns the conversion from video ID to media URL."""
    target = youtube_transcription_target("video")

    assert_eq(target.key, "youtube:video")
    assert_eq(target.locator, "https://www.youtube.com/watch?v=video")
