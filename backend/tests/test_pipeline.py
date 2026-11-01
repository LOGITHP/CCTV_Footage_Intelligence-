from pathlib import Path

import numpy as np

from video_intelligence.domain import Zone
from video_intelligence.pipeline import BrightBlobDetector, CentroidTracker
from video_intelligence.pipeline import process_video


def test_bright_blob_detector_and_tracker_produce_stable_local_track():
    frame = np.zeros((80, 100, 3), dtype=np.uint8)
    frame[20:35, 10:30] = 255
    detector = BrightBlobDetector(threshold=200, min_area=20)
    tracker = CentroidTracker(max_distance=50)

    first = tracker.update(detector.detect(frame))
    second_frame = np.zeros_like(frame)
    second_frame[22:37, 15:35] = 255
    second = tracker.update(detector.detect(second_frame))

    assert len(first) == len(second) == 1
    assert first[0].object_id == second[0].object_id
    assert first[0].x < second[0].x
    assert first[0].bbox == (10.0, 20.0, 30.0, 35.0)


def test_process_video_reads_decoder_timestamps_and_generates_zone_event(tmp_path):
    import cv2

    source = Path(tmp_path) / "fixture.avi"
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"MJPG"), 10.0, (100, 80))
    assert writer.isOpened()
    for x in (80, 40, 80):
        frame = np.zeros((80, 100, 3), dtype=np.uint8)
        frame[20:35, x - 8 : x + 8] = 255
        writer.write(frame)
    writer.release()

    result = process_video(
        source,
        (Zone("gate", ((0, 0), (50, 0), (50, 80), (0, 80))),),
        detector=BrightBlobDetector(min_area=20),
    )

    assert len(result.observations) == 3
    assert [event.event_type for event in result.events] == ["enter", "exit"]
    assert result.events[0].timestamp_seconds >= 0
