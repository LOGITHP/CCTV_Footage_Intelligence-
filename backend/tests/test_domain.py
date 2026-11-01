import pytest

from video_intelligence import (
    FrameObservation,
    ObjectObservation,
    TimestampError,
    Zone,
    generate_zone_events,
    normalize_timestamp,
    normalize_timestamps,
)


def person(timestamp, x, y):
    return FrameObservation(timestamp, (ObjectObservation("person-1", x, y),))


def test_normalization_preserves_variable_frame_timing():
    assert normalize_timestamps([0, 9000, 22500, 27000], time_base=1 / 90000) == pytest.approx(
        (0.0, 0.1, 0.25, 0.3)
    )
    assert normalize_timestamp(1500, time_base=1 / 1000) == pytest.approx(1.5)


@pytest.mark.parametrize("value", [None, -1, float("nan"), float("inf"), True])
def test_invalid_timestamps_are_rejected(value):
    with pytest.raises(TimestampError):
        normalize_timestamp(value)


def test_decreasing_timestamps_are_rejected():
    with pytest.raises(TimestampError):
        normalize_timestamps([0, 2, 1])


def test_zone_event_generation_is_idempotent_and_order_independent():
    zone = Zone("loading", ((0, 0), (10, 0), (10, 10), (0, 10)))
    observations = [person(0, 20, 5), person(1, 5, 5), person(2.5, 15, 5)]
    expected = generate_zone_events(observations, [zone])
    repeated = generate_zone_events(tuple(observations), [zone])
    reordered_zone_input = generate_zone_events(observations, reversed([zone]))

    assert expected == repeated == reordered_zone_input
    assert [(event.event_type, event.timestamp_seconds) for event in expected] == [
        ("enter", 1.0),
        ("exit", 2.5),
    ]
