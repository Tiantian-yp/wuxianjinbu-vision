import click
import yaml
import os
import sys
from typing import Optional

from src.segmenter import VideoSegmenter
from src.video_cutter import VideoCutter


def load_config(config_path: str) -> dict:
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    return {}


@click.group()
def cli():
    pass


@cli.command('analyze')
@click.option('--input', '-i', required=True, help='Input video file path')
@click.option('--output', '-o', help='Output directory for segments JSON')
@click.option('--config', '-c', default='config.yaml', help='Config file path')
@click.option('--min-duration', type=float, help='Minimum segment duration in seconds')
@click.option('--max-duration', type=float, help='Maximum segment duration in seconds')
@click.option('--tracknet', is_flag=True, help='Use TrackNetV3 for shuttlecock detection')
def analyze_video(input: str, output: str, config: str, min_duration: Optional[float],
                  max_duration: Optional[float], tracknet: bool):
    if not os.path.exists(input):
        click.echo(f"Error: Input file '{input}' does not exist", err=True)
        sys.exit(1)

    cfg = load_config(config)
    video_cfg = cfg.get('video', {})

    seg_min_duration = min_duration if min_duration is not None else video_cfg.get('min_segment_duration', 3.0)
    seg_max_duration = max_duration if max_duration is not None else video_cfg.get('max_segment_duration', 60.0)
    padding_before = video_cfg.get('padding_before', 0.5)
    padding_after = video_cfg.get('padding_after', 0.5)

    segmenter = VideoSegmenter(
        min_segment_duration=seg_min_duration,
        max_segment_duration=seg_max_duration,
        padding_before=padding_before,
        padding_after=padding_after
    )

    click.echo(f"Analyzing video: {input}")
    click.echo(f"Segment duration range: {seg_min_duration}s - {seg_max_duration}s")
    click.echo(f"Using TrackNetV3: {tracknet}")

    segments = segmenter.process_video(input, use_tracknet=tracknet, sample_interval=2)

    stats = segmenter.get_segment_stats()
    click.echo(f"\nAnalysis complete!")
    click.echo(f"Total segments detected: {stats['total_segments']}")
    click.echo(f"Total duration: {stats['total_duration']:.1f}s")
    click.echo(f"Average duration: {stats['avg_duration']:.1f}s")
    click.echo(f"Min duration: {stats['min_duration']:.1f}s")
    click.echo(f"Max duration: {stats['max_duration']:.1f}s")

    if output:
        os.makedirs(output, exist_ok=True)
        segments_file = os.path.join(output, 'segments.json')
        segmenter.save_segments_to_file(segments_file)
        click.echo(f"Segments saved to: {segments_file}")
    else:
        segments_file = os.path.splitext(input)[0] + '_segments.json'
        segmenter.save_segments_to_file(segments_file)
        click.echo(f"Segments saved to: {segments_file}")


@cli.command('cut')
@click.option('--input', '-i', required=True, help='Input video file path')
@click.option('--segments', '-s', help='Segments JSON file path')
@click.option('--output', '-o', required=True, help='Output directory for cut segments')
@click.option('--config', '-c', default='config.yaml', help='Config file path')
@click.option('--min-duration', type=float, help='Minimum segment duration filter in seconds')
@click.option('--max-duration', type=float, help='Maximum segment duration filter in seconds')
@click.option('--prefix', default='segment', help='Output file prefix')
def cut_video(input: str, segments: str, output: str, config: str, min_duration: Optional[float],
              max_duration: Optional[float], prefix: str):
    if not os.path.exists(input):
        click.echo(f"Error: Input file '{input}' does not exist", err=True)
        sys.exit(1)

    if segments is None:
        segments = os.path.splitext(input)[0] + '_segments.json'

    if not os.path.exists(segments):
        click.echo(f"Error: Segments file '{segments}' does not exist. Run 'analyze' first.", err=True)
        sys.exit(1)

    cfg = load_config(config)
    output_cfg = cfg.get('output', {})

    video_cutter = VideoCutter(
        output_format=output_cfg.get('format', 'mp4'),
        codec=output_cfg.get('codec', 'libx264'),
        quality=output_cfg.get('quality', 23)
    )

    segmenter = VideoSegmenter()
    segmenter.load_segments_from_file(segments)

    click.echo(f"Cutting video: {input}")
    click.echo(f"Output directory: {output}")

    success_count = video_cutter.cut_segments_with_filter(
        input_path=input,
        segments=segmenter.segments,
        output_dir=output,
        min_duration=min_duration,
        max_duration=max_duration,
        prefix=prefix
    )

    click.echo(f"\nCutting complete!")
    click.echo(f"Successfully cut {success_count} segments")


@cli.command('auto')
@click.option('--input', '-i', required=True, help='Input video file path')
@click.option('--output', '-o', required=True, help='Output directory for cut segments')
@click.option('--config', '-c', default='config.yaml', help='Config file path')
@click.option('--min-duration', type=float, help='Minimum segment duration in seconds')
@click.option('--max-duration', type=float, help='Maximum segment duration in seconds')
@click.option('--prefix', default='segment', help='Output file prefix')
@click.option('--tracknet', is_flag=True, help='Use TrackNetV3 for shuttlecock detection')
@click.option('--sample-interval', '-s', type=int, default=2, help='Sample interval for TrackNetV3 (1=every frame, 2=every other frame)')
def auto_process(input: str, output: str, config: str, min_duration: Optional[float],
                 max_duration: Optional[float], prefix: str, tracknet: bool, sample_interval: int):
    if not os.path.exists(input):
        click.echo(f"Error: Input file '{input}' does not exist", err=True)
        sys.exit(1)

    cfg = load_config(config)
    video_cfg = cfg.get('video', {})
    output_cfg = cfg.get('output', {})

    seg_min_duration = min_duration if min_duration is not None else video_cfg.get('min_segment_duration', 3.0)
    seg_max_duration = max_duration if max_duration is not None else video_cfg.get('max_segment_duration', 60.0)
    padding_before = video_cfg.get('padding_before', 0.5)
    padding_after = video_cfg.get('padding_after', 0.5)

    click.echo("=== Step 1: Analyzing video ===")
    click.echo(f"Using TrackNetV3: {tracknet}")
    if tracknet:
        click.echo(f"Sample interval: {sample_interval}")
    segmenter = VideoSegmenter(
        min_segment_duration=seg_min_duration,
        max_segment_duration=seg_max_duration,
        padding_before=padding_before,
        padding_after=padding_after
    )

    segments = segmenter.process_video(input, use_tracknet=tracknet, sample_interval=sample_interval)

    stats = segmenter.get_segment_stats()
    click.echo(f"\nAnalysis complete!")
    click.echo(f"Total segments detected: {stats['total_segments']}")
    click.echo(f"Total duration: {stats['total_duration']:.1f}s")
    click.echo(f"Average duration: {stats['avg_duration']:.1f}s")

    segments_file = os.path.splitext(input)[0] + '_segments.json'
    segmenter.save_segments_to_file(segments_file)

    click.echo("\n=== Step 2: Cutting video segments ===")
    video_cutter = VideoCutter(
        output_format=output_cfg.get('format', 'mp4'),
        codec=output_cfg.get('codec', 'libx264'),
        quality=output_cfg.get('quality', 23)
    )

    success_count = video_cutter.cut_segments_with_filter(
        input_path=input,
        segments=segmenter.segments,
        output_dir=output,
        min_duration=min_duration,
        max_duration=max_duration,
        prefix=prefix
    )

    click.echo(f"\n=== Process complete! ===")
    click.echo(f"Successfully cut {success_count} segments to: {output}")


@cli.command('convert')
@click.option('--input', '-i', required=True, help='Input video file path (e.g., .mov, .qt)')
@click.option('--output', '-o', help='Output MP4 file path')
@click.option('--config', '-c', default='config.yaml', help='Config file path')
def convert_to_mp4(input: str, output: str, config: str):
    if not os.path.exists(input):
        click.echo(f"Error: Input file '{input}' does not exist", err=True)
        sys.exit(1)

    cfg = load_config(config)
    output_cfg = cfg.get('output', {})

    video_cutter = VideoCutter(
        codec=output_cfg.get('codec', 'libx264'),
        quality=output_cfg.get('quality', 23)
    )

    click.echo(f"Converting: {input}")

    success = video_cutter.convert_to_mp4(input_path=input, output_path=output)

    if success:
        click.echo(f"Conversion complete!")
    else:
        click.echo("Conversion failed!", err=True)
        sys.exit(1)


@cli.command('info')
@click.option('--input', '-i', required=True, help='Input video file path')
def video_info(input: str):
    if not os.path.exists(input):
        click.echo(f"Error: Input file '{input}' does not exist", err=True)
        sys.exit(1)

    video_cutter = VideoCutter()
    info = video_cutter.get_video_info(input)

    if info:
        click.echo(f"Video Information:")
        click.echo(f"  Duration: {info['duration']:.1f} seconds")
        click.echo(f"  Size: {info['size'] / 1024 / 1024:.2f} MB")
        click.echo(f"  Bitrate: {info['bit_rate'] / 1000 if info['bit_rate'] else 'N/A'} kbps")
        click.echo(f"  Codec: {info['video_codec']}")
        click.echo(f"  Resolution: {info['width']}x{info['height']}")
        click.echo(f"  FPS: {info['fps']}")


if __name__ == '__main__':
    cli()