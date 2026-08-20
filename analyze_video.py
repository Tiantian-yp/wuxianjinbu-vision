"""
视频分析脚本 - v0.1.0-hotfix
使用方法: python analyze_video.py <视频路径> [输出目录]
"""
import cv2
import os
import sys
import argparse
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.court_detector import CourtDetector
from src.shuttlecock_tracker import ShuttlecockTracker
from src.player_pose import PlayerPoseDetector


def main():
    parser = argparse.ArgumentParser(description='羽毛球视频分析工具')
    parser.add_argument('video_path', nargs='?', help='视频文件路径')
    parser.add_argument('-o', '--output', default='./analysis_output', help='输出目录 (默认: ./analysis_output)')
    parser.add_argument('--detector', default='simple', choices=['simple', 'tracknet', 'yolo'], help='羽毛球检测器类型')
    args = parser.parse_args()

    # v0.1.0-hotfix: 如果没有提供视频路径，给出提示而不是硬编码
    if not args.video_path:
        print("=" * 60)
        print("羽毛球视频分析工具")
        print("=" * 60)
        print("使用方法:")
        print("  python analyze_video.py <视频文件路径>")
        print("")
        print("示例:")
        print("  python analyze_video.py ./my_video.mp4")
        print("  python analyze_video.py ./my_video.mp4 -o ./output")
        print("  python analyze_video.py ./my_video.mp4 --detector tracknet")
        print("")
        print("请提供视频文件路径作为参数！")
        print("=" * 60)
        sys.exit(1)

    video_path = args.video_path
    output_dir = args.output

    if not os.path.exists(video_path):
        print(f"错误: 视频文件不存在: {video_path}")
        sys.exit(1)

    os.makedirs(output_dir, exist_ok=True)
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    if total_frames <= 0:
        print(f"错误: 无法读取视频或视频为空: {video_path}")
        cap.release()
        sys.exit(1)

    court_detector = CourtDetector()
    shuttle_tracker = ShuttlecockTracker(detector_type=args.detector, fps=fps)
    pose_detector = PlayerPoseDetector(model_complexity=0)
    frame_idx = 0
    shuttle_detections = []
    pose_results = []
    court_found = False

    print(f"Analyzing video: {video_path}")
    print(f"Output dir: {output_dir}")
    print(f"Total frames: {total_frames}, FPS: {fps:.2f}")
    print(f"Detector: {args.detector}")
    print("Press 'q' to quit, 's' to save current frame, SPACE to pause/resume")
    print("-" * 60)

    paused = False
    while cap.isOpened():
        if not paused:
            ret, frame = cap.read()
            if not ret:
                break

            if frame_idx == 0:
                corners = court_detector.detect_court(frame)
                if corners is not None:
                    court_found = True
                    print(f"Court detected with {len(corners)} corners")

            if frame_idx % 2 == 0:
                shuttle_detection = shuttle_tracker.process_frame(frame)
                if shuttle_detection:
                    shuttle_detections.append(shuttle_detection)
                pose_result = pose_detector.detect(frame)
                if pose_result:
                    pose_results.append(pose_result)

        display_frame = frame.copy() if frame is not None else np.zeros((480, 640, 3), dtype=np.uint8)

        if court_found and court_detector.court_corners is not None:
            display_frame = court_detector.draw_court(display_frame)
        if frame_idx % 2 == 0 and shuttle_detection:
            cv2.circle(display_frame,
                       (int(shuttle_detection.x), int(shuttle_detection.y)),
                       10, (0, 0, 255), -1)
            cv2.putText(display_frame, f"Shuttle: {shuttle_detection.confidence:.2f}",
                        (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
        if frame_idx % 2 == 0 and pose_result:
            if pose_result.is_serving:
                cv2.putText(display_frame, "SERVING", (10, 60),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
            cv2.putText(display_frame, f"Pose conf: {pose_result.confidence:.2f}",
                        (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
        cv2.putText(display_frame, f"Frame: {frame_idx}/{total_frames}" + (" [PAUSED]" if paused else ""),
                    (10, display_frame.shape[0] - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 2)
        cv2.imshow("Analysis", display_frame)
        key = cv2.waitKey(1) & 0xFF
        if key == ord('q'):
            break
        elif key == ord(' '):
            paused = not paused
            print("Paused" if paused else "Resumed")
        elif key == ord('s'):
            save_path = os.path.join(output_dir, f"frame_{frame_idx:04d}.jpg")
            cv2.imwrite(save_path, frame)
            print(f"Saved frame {frame_idx} to {save_path}")

        if not paused:
            frame_idx += 1

    cap.release()
    cv2.destroyAllWindows()

    print("\n" + "=" * 60)
    print(f"Analysis summary:")
    print(f"  Total frames processed: {frame_idx}")
    print(f"  Shuttlecock detections: {len(shuttle_detections)}")
    print(f"  Pose detections: {len(pose_results)}")
    print(f"  Court found: {court_found}")
    if shuttle_detections:
        print("\nShuttlecock detection stats:")
        confidences = [d.confidence for d in shuttle_detections]
        print(f"   Avg confidence: {np.mean(confidences):.2f}")
        print(f"   Max confidence: {max(confidences):.2f}")
        print(f"   Min confidence: {min(confidences):.2f}")
    if pose_results:
        serving_count = sum(1 for p in pose_results if p.is_serving)
        print(f"\nPose stats:")
        print(f"   Serving detected: {serving_count}/{len(pose_results)}")
    print(f"\nOutput saved to: {os.path.abspath(output_dir)}")
    print("=" * 60)


if __name__ == '__main__':
    main()
