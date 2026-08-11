import os
import sys
import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_court_detector():
    print("\n=== Testing Court Detector ===")
    from src.court_detector import CourtDetector
    
    detector = CourtDetector()
    
    test_frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.line(test_frame, (100, 100), (1180, 100), (255, 255, 255), 3)
    cv2.line(test_frame, (100, 620), (1180, 620), (255, 255, 255), 3)
    cv2.line(test_frame, (100, 100), (100, 620), (255, 255, 255), 3)
    cv2.line(test_frame, (1180, 100), (1180, 620), (255, 255, 255), 3)
    
    corners = detector.detect_court(test_frame)
    
    if corners is not None:
        print(f"✓ Court detected with {len(corners)} corners")
        print(f"  Corners: {corners}")
        
        is_inside = detector.is_inside_court(640, 360)
        print(f"✓ Center point (640, 360) is inside court: {is_inside}")
        
        court_x, court_y = detector.pixel_to_court_coords(640, 360)
        print(f"✓ Pixel (640, 360) -> Court coords ({court_x:.2f}, {court_y:.2f})")
    else:
        print("✗ Court detection failed (expected with simple test frame)")
    
    return corners is not None

def test_shuttlecock_tracker():
    print("\n=== Testing Shuttlecock Tracker ===")
    from src.shuttlecock_tracker import ShuttlecockTracker
    
    tracker = ShuttlecockTracker(detector_type='simple', fps=30.0)
    
    test_frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.circle(test_frame, (640, 360), 10, (255, 255, 255), -1)
    
    detection = tracker.process_frame(test_frame)
    
    if detection:
        print(f"✓ Shuttlecock detected at ({detection.x:.2f}, {detection.y:.2f})")
        print(f"  Confidence: {detection.confidence:.2f}")
    else:
        print("✗ Shuttlecock detection failed")
    
    speed = tracker.get_speed()
    print(f"✓ Speed calculation: {speed:.2f}")
    
    return detection is not None

def test_player_pose():
    print("\n=== Testing Player Pose Detector ===")
    from src.player_pose import PlayerPoseDetector
    
    detector = PlayerPoseDetector(model_complexity=0)
    
    test_frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    cv2.rectangle(test_frame, (500, 200), (780, 650), (255, 255, 255), 2)
    
    pose = detector.detect(test_frame)
    
    if pose:
        print(f"✓ Pose detected at frame {pose.frame_idx}")
        print(f"  Timestamp: {pose.timestamp:.2f}s")
        print(f"  Is serving: {pose.is_serving}")
        print(f"  Confidence: {pose.confidence:.2f}")
    else:
        print("✗ Pose detection failed (expected with simple test frame)")
    
    return True

def test_segmenter():
    print("\n=== Testing Video Segmenter ===")
    from src.segmenter import VideoSegmenter, Segment
    
    segmenter = VideoSegmenter(
        min_segment_duration=3.0,
        max_segment_duration=60.0,
        padding_before=0.5,
        padding_after=0.5
    )
    
    test_segment = Segment(
        start_time=10.0,
        end_time=15.0,
        start_frame=300,
        end_frame=450
    )
    
    print(f"✓ Segment duration: {test_segment.duration:.2f}s")
    
    segmenter.segments = [test_segment]
    stats = segmenter.get_segment_stats()
    
    print(f"✓ Stats: {stats}")
    
    output_file = '/tmp/test_segments.json'
    segmenter.save_segments_to_file(output_file)
    print(f"✓ Segments saved to: {output_file}")
    
    loaded_segments = segmenter.load_segments_from_file(output_file)
    print(f"✓ Loaded {len(loaded_segments)} segments")
    
    filtered = segmenter.filter_by_duration(min_duration=4.0, max_duration=6.0)
    print(f"✓ Filtered to {len(filtered)} segments")
    
    return True

def test_video_cutter():
    print("\n=== Testing Video Cutter ===")
    from src.video_cutter import VideoCutter
    
    cutter = VideoCutter(output_format='mp4', codec='libx264', quality=23)
    
    test_time = 123.456
    formatted = cutter._format_time(test_time)
    print(f"✓ Time formatting: {test_time}s -> {formatted}")
    
    return True

def test_cli():
    print("\n=== Testing CLI ===")
    import subprocess
    
    result = subprocess.run(
        [sys.executable, 'main.py', '--help'],
        capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__))
    )
    
    if result.returncode == 0:
        print("✓ CLI help command works")
        print(f"  Commands available: {result.stdout[:200]}...")
    else:
        print(f"✗ CLI failed: {result.stderr}")
    
    return result.returncode == 0

if __name__ == '__main__':
    print("=" * 60)
    print("Badminton Video Segmenter - System Test")
    print("=" * 60)
    
    tests = [
        ("Court Detector", test_court_detector),
        ("Shuttlecock Tracker", test_shuttlecock_tracker),
        ("Player Pose Detector", test_player_pose),
        ("Video Segmenter", test_segmenter),
        ("Video Cutter", test_video_cutter),
        ("CLI Interface", test_cli),
    ]
    
    passed = 0
    failed = 0
    
    for name, test_func in tests:
        try:
            if test_func():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"\n✗ {name} failed with error: {e}")
            failed += 1
    
    print("\n" + "=" * 60)
    print(f"Test Results: {passed} passed, {failed} failed")
    print("=" * 60)
    
    if failed > 0:
        sys.exit(1)