import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.video_cutter import VideoCutter
from src.segmenter import Segment

def test_crop():
    input_video = '/tmp/test_video.mp4'
    output_dir = '/tmp/test_output'
    
    os.makedirs(output_dir, exist_ok=True)
    
    segments = [
        Segment(start_time=5.0, end_time=15.0, start_frame=150, end_frame=450),
        Segment(start_time=20.0, end_time=35.0, start_frame=600, end_frame=1050),
        Segment(start_time=40.0, end_time=55.0, start_frame=1200, end_frame=1650),
    ]
    
    print(f"Testing video cutting with {len(segments)} segments")
    print(f"Input: {input_video}")
    print(f"Output: {output_dir}")
    
    cutter = VideoCutter(output_format='mp4', codec='libx264', quality=23)
    
    success_count = cutter.cut_all_segments(input_video, segments, output_dir, prefix='test')
    
    print(f"\nCutting complete! Successfully cut {success_count} segments")
    
    output_files = os.listdir(output_dir)
    for f in output_files:
        filepath = os.path.join(output_dir, f)
        filesize = os.path.getsize(filepath) / 1024 / 1024
        print(f"  - {f} ({filesize:.2f} MB)")
    
    return success_count == len(segments)

if __name__ == '__main__':
    print("=" * 60)
    print("Video Cutting Test")
    print("=" * 60)
    
    success = test_crop()
    
    print("\n" + "=" * 60)
    print(f"Test {'PASSED' if success else 'FAILED'}")
    print("=" * 60)
    
    sys.exit(0 if success else 1)