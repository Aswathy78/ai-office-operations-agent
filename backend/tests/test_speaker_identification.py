from app.meeting.speaker_identification import (
    UNKNOWN_SPEAKER,
    identify_recording_speaker,
)


def test_known_participant_is_identified():
    speaker = identify_recording_speaker(
        participant_name="Rahul",
    )

    assert speaker.name == "Rahul"
    assert speaker.flagged_for_correction is False


def test_missing_participant_becomes_unknown():
    speaker = identify_recording_speaker(
        participant_name=None,
    )

    assert speaker.name == UNKNOWN_SPEAKER
    assert speaker.flagged_for_correction is True


def test_blank_participant_becomes_unknown():
    speaker = identify_recording_speaker(
        participant_name="   ",
    )

    assert speaker.name == UNKNOWN_SPEAKER
    assert speaker.flagged_for_correction is True


def test_participant_name_is_trimmed():
    speaker = identify_recording_speaker(
        participant_name="  Rahul  ",
    )

    assert speaker.name == "Rahul"
    assert speaker.flagged_for_correction is False