"""Tests for kombucha.vision — frame delta, self-model error, YOLO, tracker."""

import math

import pytest

from kombucha.config import MotionConfig
from kombucha.vision import (
    compute_basic_self_model_error,
    compute_self_model_error,
    compute_frame_delta,
    Detection,
    TrackedObject,
    CentroidTracker,
    estimate_distance,
    pixel_to_bearing,
    classify_motion,
    build_scene_state,
    COCO_CLASSES,
    CLASSES_OF_INTEREST,
)


class TestFrameDelta:
    def test_none_if_no_prev_frame(self):
        assert compute_frame_delta(None, "abc") is None

    def test_none_if_no_curr_frame(self):
        assert compute_frame_delta("abc", None) is None

    def test_none_if_both_none(self):
        assert compute_frame_delta(None, None) is None


class TestBasicSelfModelError:
    def test_no_actions_no_frames(self):
        error = compute_basic_self_model_error([], None, None)
        assert error["frame_delta"] is None
        assert error["anomaly"] is False

    def test_drive_expected_motion_flag(self):
        actions = [{"type": "drive", "left": 0.5, "right": 0.5}]
        error = compute_basic_self_model_error(actions, None, None)
        # No delta (no frames), so flags stay at default
        assert error["drive_expected_motion"] is False

    def test_look_expected_change_flag(self):
        actions = [{"type": "look", "pan": 45}]
        error = compute_basic_self_model_error(actions, None, None)
        assert error["look_expected_change"] is False

    def test_with_config(self):
        config = MotionConfig(frame_delta_threshold=0.02, anomaly_threshold=0.1)
        error = compute_basic_self_model_error([], None, None, motion_config=config)
        assert error["anomaly"] is False


class TestFullSelfModelError:
    def test_gimbal_error_pan(self):
        actions = [{"type": "look", "pan": 90, "tilt": 0}]
        error = compute_self_model_error(
            actions, None, None,
            prev_pan=0, curr_pan=50,
        )
        assert error.get("gimbal_error_pan") == 40

    def test_gimbal_error_tilt(self):
        actions = [{"type": "look", "pan": 0, "tilt": 45}]
        error = compute_self_model_error(
            actions, None, None,
            prev_tilt=0, curr_tilt=20,
        )
        assert error.get("gimbal_error_tilt") == 25

    def test_gimbal_anomaly_on_large_pan_error(self):
        actions = [{"type": "look", "pan": 90, "tilt": 0}]
        error = compute_self_model_error(
            actions, None, None,
            prev_pan=0, curr_pan=0,
        )
        assert error["anomaly"] is True
        assert "gimbal_pan_error" in error["anomaly_reason"]

    def test_no_gimbal_error_when_no_look(self):
        actions = [{"type": "drive", "left": 0.3, "right": 0.3}]
        error = compute_self_model_error(
            actions, None, None,
            prev_pan=0, curr_pan=0,
        )
        assert error.get("gimbal_error_pan") is None


# ===========================================================================
# Detection dataclass
# ===========================================================================

class TestDetection:
    def test_create_detection(self):
        det = Detection(
            cls="person", confidence=0.95,
            bbox=(10, 20, 100, 200), centroid=(55, 110), area=16200.0,
        )
        assert det.cls == "person"
        assert det.confidence == 0.95
        assert det.bbox == (10, 20, 100, 200)
        assert det.centroid == (55, 110)
        assert det.area == 16200.0


# ===========================================================================
# COCO classes
# ===========================================================================

class TestCocoClasses:
    def test_has_80_classes(self):
        assert len(COCO_CLASSES) == 80

    def test_person_is_first(self):
        assert COCO_CLASSES[0] == "person"

    def test_classes_of_interest_subset(self):
        for cls in CLASSES_OF_INTEREST:
            # "door" is not in COCO but is in our interest set — that's fine
            if cls != "door":
                assert cls in COCO_CLASSES, f"{cls} not in COCO_CLASSES"


# ===========================================================================
# Centroid Tracker
# ===========================================================================

class TestCentroidTracker:
    def test_register_single_detection(self):
        tracker = CentroidTracker()
        dets = [Detection("person", 0.9, (10, 10, 50, 50), (30, 30), 1600)]
        result = tracker.update(dets)
        assert len(result) == 1
        assert result[0].track_id == 0
        assert result[0].cls == "person"

    def test_persistent_id_across_frames(self):
        tracker = CentroidTracker()
        d1 = [Detection("person", 0.9, (10, 10, 50, 50), (30, 30), 1600)]
        tracker.update(d1)
        # Same position, next frame
        d2 = [Detection("person", 0.88, (12, 12, 52, 52), (32, 32), 1600)]
        result = tracker.update(d2)
        assert len(result) == 1
        assert result[0].track_id == 0  # same ID
        assert result[0].age == 2

    def test_multiple_objects_tracked(self):
        tracker = CentroidTracker()
        dets = [
            Detection("person", 0.9, (10, 10, 50, 50), (30, 30), 1600),
            Detection("cat", 0.8, (200, 200, 250, 250), (225, 225), 2500),
        ]
        result = tracker.update(dets)
        assert len(result) == 2
        ids = {r.track_id for r in result}
        assert len(ids) == 2

    def test_disappeared_object_removed(self):
        tracker = CentroidTracker(max_disappeared=2)
        dets = [Detection("person", 0.9, (10, 10, 50, 50), (30, 30), 1600)]
        tracker.update(dets)
        # Object disappears for 3 frames
        tracker.update([])
        tracker.update([])
        result = tracker.update([])
        assert len(result) == 0

    def test_disappeared_object_kept_during_grace(self):
        tracker = CentroidTracker(max_disappeared=3)
        dets = [Detection("person", 0.9, (10, 10, 50, 50), (30, 30), 1600)]
        tracker.update(dets)
        # Object disappears for 2 frames (within grace)
        result = tracker.update([])
        assert len(result) == 1
        assert result[0].disappeared == 1

    def test_new_detection_gets_new_id(self):
        tracker = CentroidTracker(max_distance=50)
        d1 = [Detection("person", 0.9, (10, 10, 50, 50), (30, 30), 1600)]
        tracker.update(d1)
        # Far away detection — should get new ID
        d2 = [
            Detection("person", 0.9, (12, 12, 52, 52), (32, 32), 1600),
            Detection("cat", 0.8, (400, 400, 450, 450), (425, 425), 2500),
        ]
        result = tracker.update(d2)
        assert len(result) == 2
        ids = sorted([r.track_id for r in result])
        assert ids == [0, 1]

    def test_max_distance_prevents_match(self):
        tracker = CentroidTracker(max_distance=20)
        d1 = [Detection("person", 0.9, (10, 10, 50, 50), (30, 30), 1600)]
        tracker.update(d1)
        # Detection far away — should be new ID, old one disappears
        d2 = [Detection("person", 0.9, (300, 300, 350, 350), (325, 325), 2500)]
        result = tracker.update(d2)
        # Old one is disappeared but still tracked (within grace period)
        assert len(result) == 2
        disappeared = [r for r in result if r.disappeared > 0]
        assert len(disappeared) == 1


# ===========================================================================
# Scene Builder Helpers
# ===========================================================================

class TestEstimateDistance:
    def test_reference_distance(self):
        # At ref height, should return ref distance
        dist = estimate_distance((0, 0, 0, 300), frame_height=480,
                                 ref_height_px=300, ref_distance_m=1.5)
        assert dist == 1.5

    def test_closer_object_larger_bbox(self):
        # Object taking up 600px height — closer than reference
        dist = estimate_distance((0, 0, 0, 600), frame_height=480,
                                 ref_height_px=300, ref_distance_m=1.5)
        assert dist < 1.5

    def test_farther_object_smaller_bbox(self):
        # Object taking up 150px height — farther than reference
        dist = estimate_distance((0, 0, 0, 150), frame_height=480,
                                 ref_height_px=300, ref_distance_m=1.5)
        assert dist > 1.5

    def test_tiny_object_returns_max(self):
        dist = estimate_distance((0, 0, 0, 3))
        assert dist == 10.0


class TestPixelToBearing:
    def test_center_is_zero(self):
        bearing = pixel_to_bearing(320, frame_width=640, fov_deg=160)
        assert bearing == 0.0

    def test_left_edge_negative(self):
        bearing = pixel_to_bearing(0, frame_width=640, fov_deg=160)
        assert bearing == -80.0

    def test_right_edge_positive(self):
        bearing = pixel_to_bearing(640, frame_width=640, fov_deg=160)
        assert bearing == 80.0

    def test_quarter_left(self):
        bearing = pixel_to_bearing(160, frame_width=640, fov_deg=160)
        assert bearing == -40.0


class TestClassifyMotion:
    def test_new_with_few_centroids(self):
        obj = TrackedObject(
            track_id=0, cls="person", centroid=(100, 100),
            bbox=(80, 80, 120, 120), confidence=0.9, area=1600,
            prev_centroids=[(100, 100)],
        )
        assert classify_motion(obj) == "new"

    def test_stationary(self):
        obj = TrackedObject(
            track_id=0, cls="person", centroid=(100, 100),
            bbox=(80, 80, 120, 120), confidence=0.9, area=1600,
            prev_centroids=[(100, 100), (101, 100), (100, 101)],
        )
        assert classify_motion(obj) == "stationary"

    def test_approaching(self):
        obj = TrackedObject(
            track_id=0, cls="person", centroid=(100, 150),
            bbox=(80, 80, 120, 200), confidence=0.9, area=1600,
            prev_centroids=[(100, 100), (100, 120), (100, 150)],
        )
        assert classify_motion(obj) == "approaching"

    def test_receding(self):
        obj = TrackedObject(
            track_id=0, cls="person", centroid=(100, 50),
            bbox=(80, 30, 120, 70), confidence=0.9, area=1600,
            prev_centroids=[(100, 100), (100, 80), (100, 50)],
        )
        assert classify_motion(obj) == "receding"

    def test_moving_horizontal(self):
        obj = TrackedObject(
            track_id=0, cls="person", centroid=(200, 100),
            bbox=(180, 80, 220, 120), confidence=0.9, area=1600,
            prev_centroids=[(100, 100), (150, 100), (200, 100)],
        )
        assert classify_motion(obj) == "moving"


# ===========================================================================
# Build Scene State
# ===========================================================================

class TestBuildSceneState:
    def test_empty_tracked_objects(self):
        scene = build_scene_state("base64data", 0.01, [], motion_threshold=0.03)
        assert len(scene.objects) == 0
        assert scene.person_count == 0
        assert scene.motion_detected is False

    def test_motion_detected_above_threshold(self):
        scene = build_scene_state("base64data", 0.05, [], motion_threshold=0.03)
        assert scene.motion_detected is True

    def test_with_tracked_person(self):
        tracked = TrackedObject(
            track_id=1, cls="person", centroid=(320, 240),
            bbox=(200, 100, 440, 400), confidence=0.92, area=72000,
            prev_centroids=[(320, 240)],
        )
        scene = build_scene_state("base64data", 0.01, [tracked])
        assert len(scene.objects) == 1
        assert scene.person_count == 1
        obj = scene.objects[0]
        assert obj.cls == "person"
        assert obj.track_id == 1
        assert obj.confidence == 0.92
        assert obj.bearing_deg == 0.0  # centered

    def test_disappeared_objects_excluded(self):
        tracked = TrackedObject(
            track_id=1, cls="person", centroid=(320, 240),
            bbox=(200, 100, 440, 400), confidence=0.92, area=72000,
            disappeared=1,  # not seen this frame
        )
        scene = build_scene_state("base64data", 0.01, [tracked])
        assert len(scene.objects) == 0
        assert scene.person_count == 0

    def test_multiple_objects_with_person_count(self):
        t1 = TrackedObject(
            track_id=0, cls="person", centroid=(100, 200),
            bbox=(50, 100, 150, 300), confidence=0.9, area=20000,
        )
        t2 = TrackedObject(
            track_id=1, cls="cat", centroid=(400, 300),
            bbox=(350, 250, 450, 350), confidence=0.85, area=10000,
        )
        t3 = TrackedObject(
            track_id=2, cls="person", centroid=(500, 200),
            bbox=(450, 100, 550, 300), confidence=0.88, area=20000,
        )
        scene = build_scene_state("base64data", 0.01, [t1, t2, t3])
        assert len(scene.objects) == 3
        assert scene.person_count == 2

    def test_frame_b64_preserved(self):
        scene = build_scene_state("my_frame_data", 0.0, [])
        assert scene.frame_b64 == "my_frame_data"
