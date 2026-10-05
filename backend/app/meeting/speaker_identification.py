"""Speaker identification for meeting transcription.

Maps transcript segments to the participant associated with the
recording/audio track.
"""

from dataclasses import dataclass


UNKNOWN_SPEAKER = "Unknown Speaker"


@dataclass(frozen=True)
class SpeakerIdentity:
    """Identity of the participant associated with an audio recording."""

    name: str
    flagged_for_correction: bool = False


def identify_recording_speaker(
    *,
    participant_name: str | None,
) -> SpeakerIdentity:
    """Resolve the speaker associated with a recording.

    If the recording is linked to a known participant, return that
    participant's name.

    If no participant can be resolved, return Unknown Speaker and
    flag the result for correction.
    """

    if participant_name and participant_name.strip():
        return SpeakerIdentity(
            name=participant_name.strip(),
            flagged_for_correction=False,
        )

    return SpeakerIdentity(
        name=UNKNOWN_SPEAKER,
        flagged_for_correction=True,
    )
