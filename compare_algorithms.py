import argparse
import json
import os
import time
from dataclasses import asdict
from typing import List, Optional, Tuple

import cv2
import numpy as np

from src.algorithm_schemes import probe_video, run_algorithm


def generate_synthetic_video(output_path: str, width: int = 640, height: int = 360,
                             fps: int = 30, duration: float = 12.0) -> List[Tuple[float, float]]:
    ground_truth = [(2.0, 5.5), (7.0, 11.0)]
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(output_path, fourcc, fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError(f"无法写入合成视频: {output_path}")
    total_frames = int(duration * fps)
    court_color = (54, 118, 67)
    line_color = (235, 235, 235)
    player_color = (185, 120, 70)
    for frame_idx in range(total_frames):
        timestamp = frame_idx / fps
        active = any(start <= timestamp <= end for start, end in ground_truth)
        frame = np.full((height, width, 3), court_color, dtype=np.uint8)
        cv2.rectangle(frame, (60, 35), (width - 60, height - 35), line_color, 2)
        cv2.line(frame, (width // 2, 35), (width // 2, height - 35), line_color, 2)
        cv2.line(frame, (60, height // 2), (width - 60, height // 2), line_color, 2)
        if active:
            phase = timestamp * 6.0
            ball_x = int(width // 2 + np.sin(phase) * width * 0.24)
            ball_y = int(height // 2 + np.cos(phase * 1.4) * height * 0.18)
            player_x = int(130 + (np.sin(phase * 0.7) + 1) * 80)
            player_y = int(height - 120 + np.cos(phase * 0.9) * 18)
            cv2.rectangle(frame, (player_x, player_y), (player_x + 34, player_y + 68), player_color, -1)
            cv2.circle(frame, (ball_x, ball_y), 7, (245, 245, 245), -1)
        writer.write(frame)
    writer.release()
    return ground_truth


def parse_intervals(value: Optional[str]) -> Optional[List[Tuple[float, float]]]:
    if not value:
        return None
    intervals = []
    for part in value.split(","):
        start_text, end_text = part.split(":", 1)
        intervals.append((float(start_text), float(end_text)))
    return intervals


def active_mask_from_intervals(timestamps: List[float], intervals: List[Tuple[float, float]]) -> np.ndarray:
    values = np.zeros(len(timestamps), dtype=bool)
    for index, timestamp in enumerate(timestamps):
        values[index] = any(start <= timestamp <= end for start, end in intervals)
    return values


def active_mask_from_result(result: AlgorithmResult, timestamps: List[float]) -> np.ndarray:
    values = np.zeros(len(timestamps), dtype=bool)
    for index, timestamp in enumerate(timestamps):
        values[index] = any(interval.start_time <= timestamp <= interval.end_time for interval in result.intervals)
    return values


def calculate_metrics(result: AlgorithmResult, ground_truth: List[Tuple[float, float]],
                      timestamps: List[float]) -> dict:
    predicted = active_mask_from_result(result, timestamps)
    actual = active_mask_from_intervals(timestamps, ground_truth)
    tp = int(np.logical_and(predicted, actual).sum())
    fp = int(np.logical_and(predicted, ~actual).sum())
    fn = int(np.logical_and(~predicted, actual).sum())
    tn = int(np.logical_and(~predicted, ~actual).sum())
    precision = tp / max(1, tp + fp)
    recall = tp / max(1, tp + fn)
    f1 = 2 * precision * recall / max(1e-9, precision + recall)
    intersection = int(np.logical_and(predicted, actual).sum())
    union = int(np.logical_or(predicted, actual).sum())
    iou = intersection / max(1, union)
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "iou": round(iou, 4),
    }


def summarize_result(result: AlgorithmResult) -> dict:
    durations = [item.duration for item in result.intervals]
    scores = [item.confidence for item in result.intervals]
    return {
        "name": result.name,
        "available": result.available,
        "elapsed_seconds": round(result.elapsed_seconds, 4),
        "segment_count": len(result.intervals),
        "total_active_seconds": round(float(sum(durations)), 3),
        "avg_segment_seconds": round(float(np.mean(durations)), 3) if durations else 0.0,
        "avg_confidence": round(float(np.mean(scores)), 3) if scores else 0.0,
        "notes": result.notes,
        "error": result.error,
    }


def compare(video_path: str, algorithms: List[str], sample_interval: int, min_duration: float,
            ground_truth: Optional[List[Tuple[float, float]]], vlm_keyframe_seconds: float) -> dict:
    info = probe_video(video_path)
    timestamps = []
    for frame_idx in range(0, info.total_frames, max(1, sample_interval)):
        timestamps.append(frame_idx / max(info.fps, 1.0))
    results = []
    for name in algorithms:
        kwargs = {}
        if name == "vlm_sparse":
            kwargs["keyframe_interval_seconds"] = vlm_keyframe_seconds
        result = run_algorithm(name, video_path, sample_interval=sample_interval,
                               min_duration=min_duration, **kwargs)
        results.append(result)
    report_results = []
    for result in results:
        item = summarize_result(result)
        if result.available and ground_truth:
            item["metrics"] = calculate_metrics(result, ground_truth, timestamps)
        item["intervals"] = [asdict(interval) for interval in result.intervals]
        report_results.append(item)
    return {
        "video": {
            "path": os.path.abspath(video_path),
            "fps": info.fps,
            "total_frames": info.total_frames,
            "duration_seconds": round(info.duration, 3),
            "width": info.width,
            "height": info.height,
        },
        "parameters": {
            "algorithms": algorithms,
            "sample_interval": sample_interval,
            "min_duration": min_duration,
            "vlm_keyframe_seconds": vlm_keyframe_seconds,
        },
        "ground_truth": [{"start_time": start, "end_time": end} for start, end in (ground_truth or [])],
        "results": report_results,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def print_table(report: dict):
    print("\n算法对比结果")
    print("-" * 118)
    header = f"{'方案':<16}{'可用':<6}{'耗时(s)':<10}{'片段数':<8}{'活跃时长':<10}{'平均时长':<10}{'Precision':<11}{'Recall':<10}{'F1':<9}{'IoU':<9}"
    print(header)
    print("-" * 118)
    for item in report["results"]:
        metrics = item.get("metrics") or {}
        print(
            f"{item['name']:<16}{str(item['available']):<6}{item['elapsed_seconds']:<10}"
            f"{item['segment_count']:<8}{item['total_active_seconds']:<10}{item['avg_segment_seconds']:<10}"
            f"{metrics.get('precision', '-'):<11}{metrics.get('recall', '-'):<10}"
            f"{metrics.get('f1', '-'):<9}{metrics.get('iou', '-'):<9}"
        )
        if item.get("notes"):
            print(f"  备注: {item['notes']}")
        if item.get("error"):
            print(f"  错误: {item['error']}")
    print("-" * 118)


def main():
    parser = argparse.ArgumentParser(description="羽毛球视频识别算法本地对比")
    parser.add_argument("--input", "-i", help="输入视频；不传则自动生成合成视频")
    parser.add_argument("--output", "-o", default="benchmark_output", help="报告输出目录")
    parser.add_argument("--algorithms", default="simple_hsv,motion_flow,pose_activity,tracknet_v3,vlm_sparse")
    parser.add_argument("--sample-interval", type=int, default=2)
    parser.add_argument("--min-duration", type=float, default=1.0)
    parser.add_argument("--ground-truth", help="真实回合区间，格式 2.0:5.5,7.0:11.0")
    parser.add_argument("--synthetic-duration", type=float, default=12.0)
    parser.add_argument("--vlm-keyframe-seconds", type=float, default=2.0)
    args = parser.parse_args()

    os.makedirs(args.output, exist_ok=True)
    ground_truth = parse_intervals(args.ground_truth)
    cleanup_video = False
    if args.input:
        video_path = args.input
    else:
        video_path = os.path.join(args.output, "synthetic_badminton.mp4")
        ground_truth = generate_synthetic_video(video_path, duration=args.synthetic_duration)
        cleanup_video = False

    algorithms = [item.strip() for item in args.algorithms.split(",") if item.strip()]
    report = compare(
        video_path=video_path,
        algorithms=algorithms,
        sample_interval=max(1, args.sample_interval),
        min_duration=args.min_duration,
        ground_truth=ground_truth,
        vlm_keyframe_seconds=args.vlm_keyframe_seconds,
    )

    report_path = os.path.join(args.output, "algorithm_comparison_report.json")
    with open(report_path, "w", encoding="utf-8") as file:
        json.dump(report, file, ensure_ascii=False, indent=2)
    print_table(report)
    print(f"报告已保存: {report_path}")
    if cleanup_video and os.path.exists(video_path):
        os.remove(video_path)


if __name__ == "__main__":
    main()
