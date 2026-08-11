import subprocess
import json
import os
import sys


def get_stream_info(filepath):
    command = [
        'ffprobe',
        '-v', 'quiet',
        '-print_format', 'json',
        '-show_streams',
        filepath
    ]
    
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        info = json.loads(result.stdout)
        return info.get('streams', [])
    except Exception as e:
        print(f"Error getting stream info for {filepath}: {e}")
        return []


def analyze_sync(filepath):
    print(f"\n{'='*60}")
    print(f"Analyzing: {os.path.basename(filepath)}")
    print(f"{'='*60}")
    
    streams = get_stream_info(filepath)
    
    video_stream = None
    audio_stream = None
    
    for stream in streams:
        if stream.get('codec_type') == 'video':
            video_stream = stream
        elif stream.get('codec_type') == 'audio':
            audio_stream = stream
    
    if video_stream:
        print("\nVideo Stream:")
        print(f"  Codec: {video_stream.get('codec_name', 'N/A')}")
        print(f"  Resolution: {video_stream.get('width', 'N/A')}x{video_stream.get('height', 'N/A')}")
        print(f"  FPS: {video_stream.get('r_frame_rate', 'N/A')}")
        print(f"  Duration: {video_stream.get('duration', 'N/A')}s")
        print(f"  Timebase: {video_stream.get('time_base', 'N/A')}")
        print(f"  Start PTS: {video_stream.get('start_pts', 'N/A')}")
    
    if audio_stream:
        print("\nAudio Stream:")
        print(f"  Codec: {audio_stream.get('codec_name', 'N/A')}")
        print(f"  Sample Rate: {audio_stream.get('sample_rate', 'N/A')} Hz")
        print(f"  Channels: {audio_stream.get('channels', 'N/A')}")
        print(f"  Duration: {audio_stream.get('duration', 'N/A')}s")
        print(f"  Timebase: {audio_stream.get('time_base', 'N/A')}")
        print(f"  Start PTS: {audio_stream.get('start_pts', 'N/A')}")
    
    if video_stream and audio_stream:
        video_duration = float(video_stream.get('duration', 0))
        audio_duration = float(audio_stream.get('duration', 0))
        
        print(f"\nSync Check:")
        print(f"  Video duration: {video_duration:.3f}s")
        print(f"  Audio duration: {audio_duration:.3f}s")
        print(f"  Difference: {abs(video_duration - audio_duration):.3f}s")
        
        if abs(video_duration - audio_duration) < 0.1:
            print("  ✓ Audio and video durations match")
        else:
            print("  ✗ Duration mismatch detected!")
        
        video_start = int(video_stream.get('start_pts', 0))
        audio_start = int(audio_stream.get('start_pts', 0))
        
        if video_start == 0 and audio_start == 0:
            print("  ✓ Both streams start at PTS 0")
        else:
            print(f"  ⚠ Video start PTS: {video_start}, Audio start PTS: {audio_start}")
    
    print(f"\n{'='*60}")


def check_first_frame(filepath):
    command = [
        'ffprobe',
        '-v', 'quiet',
        '-print_format', 'json',
        '-show_frames',
        '-read_intervals', '%+#1',
        '-select_streams', 'v:0',
        filepath
    ]
    
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        info = json.loads(result.stdout)
        frames = info.get('frames', [])
        
        if frames:
            frame = frames[0]
            print(f"First Video Frame:")
            print(f"  PTS: {frame.get('pkt_pts', 'N/A')}")
            print(f"  PTS Time: {frame.get('pkt_pts_time', 'N/A')}s")
            print(f"  Duration: {frame.get('pkt_duration_time', 'N/A')}s")
    except Exception as e:
        print(f"Error checking first frame: {e}")


def main():
    output_dir = './output'
    
    if not os.path.exists(output_dir):
        print(f"Output directory {output_dir} not found")
        sys.exit(1)
    
    files = sorted([f for f in os.listdir(output_dir) if f.endswith('.mp4')])
    
    if not files:
        print(f"No MP4 files found in {output_dir}")
        sys.exit(1)
    
    print(f"Found {len(files)} video segments to verify")
    print(f"{'='*60}")
    
    for filename in files:
        filepath = os.path.join(output_dir, filename)
        analyze_sync(filepath)
        check_first_frame(filepath)
    
    print("\nVerification complete!")


if __name__ == '__main__':
    main()