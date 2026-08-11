import os
import sys
import cv2
import numpy as np
import torch
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'models', 'TrackNetV3'))

from utils.general import HEIGHT, WIDTH, get_model, to_img

def predict_location(heatmap):
    if np.amax(heatmap) == 0:
        return 0, 0, 0, 0
    else:
        (cnts, _) = cv2.findContours(heatmap.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        rects = [cv2.boundingRect(ctr) for ctr in cnts]
        if len(rects) == 0:
            return 0, 0, 0, 0
        max_area_idx = 0
        max_area = rects[0][2] * rects[0][3]
        for i in range(1, len(rects)):
            area = rects[i][2] * rects[i][3]
            if area > max_area:
                max_area_idx = i
                max_area = area
        x, y, w, h = rects[max_area_idx]
        return x, y, w, h

def process_video():
    video_path = "./飞书20260714-202307.mp4"
    tracknet_path = './models/TrackNetV3/ckpts/ckpts/TrackNet_best.pt'
    
    print(f"Loading model...")
    ckpt = torch.load(tracknet_path, map_location='cpu', weights_only=True)
    seq_len = ckpt['param_dict']['seq_len']
    bg_mode = ckpt['param_dict']['bg_mode']
    
    tracknet = get_model('TrackNet', seq_len, bg_mode)
    tracknet.load_state_dict(ckpt['model'])
    tracknet.eval()
    
    print(f"seq_len={seq_len}, bg_mode={bg_mode}")
    
    cap = cv2.VideoCapture(video_path)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    
    img_scaler = (w / WIDTH, h / HEIGHT)
    print(f"Video: {w}x{h}, FPS={fps}, total_frames={total_frames}")
    
    frame_list = []
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
        frame_list.append(frame)
    cap.release()
    
    num_frames = len(frame_list)
    print(f"Loaded {num_frames} frames")
    
    detections = []
    visible_count = 0
    start_time = time.time()
    
    for i in range(0, num_frames - seq_len, seq_len):
        end_idx = min(i + seq_len + 1, num_frames)
        seq_frames = frame_list[i:end_idx]
        
        if len(seq_frames) < seq_len + 1:
            continue
        
        seq_processed = []
        for frame in seq_frames:
            frame_rgb = frame[..., ::-1]
            frame_resized = cv2.resize(frame_rgb, (WIDTH, HEIGHT))
            frame_normalized = frame_resized / 255.0
            seq_processed.append(frame_normalized)
        
        seq_processed = np.array(seq_processed)
        seq_processed = np.transpose(seq_processed, (0, 3, 1, 2))
        
        if bg_mode == 'concat':
            x = torch.from_numpy(seq_processed).float()
            x = x.reshape(1, -1, HEIGHT, WIDTH)
        else:
            x = torch.from_numpy(seq_processed[:seq_len]).float()
            x = x.reshape(1, -1, HEIGHT, WIDTH)
        
        with torch.no_grad():
            y_pred = tracknet(x)
        
        for f_idx in range(y_pred.shape[1]):
            frame_num = i + f_idx
            if frame_num >= num_frames:
                break
            
            y_p = (y_pred[:, f_idx:f_idx+1] > 0.5).detach().cpu().numpy()
            heatmap = to_img(y_p[0, 0])
            bbox_pred = predict_location(heatmap)
            cx_pred = int(bbox_pred[0] + bbox_pred[2] / 2)
            cy_pred = int(bbox_pred[1] + bbox_pred[3] / 2)
            
            cx_pred = int(cx_pred * img_scaler[0])
            cy_pred = int(cy_pred * img_scaler[1])
            visibility = 0 if cx_pred == 0 and cy_pred == 0 else 1
            
            if visibility == 1:
                visible_count += 1
            
            detections.append({
                'frame': frame_num,
                'x': cx_pred,
                'y': cy_pred,
                'visibility': visibility,
                'timestamp': frame_num / fps
            })
    
    elapsed = time.time() - start_time
    print(f"\nProcessing complete in {elapsed:.2f}s")
    print(f"Total detections: {len(detections)}")
    print(f"Visible detections: {visible_count}")
    
    if detections:
        print("\nSample detections:")
        for d in detections[:20]:
            if d['visibility'] == 1:
                print(f"  Frame {d['frame']}: ({d['x']}, {d['y']})")
    
    return detections

if __name__ == '__main__':
    print("=" * 60)
    print("TrackNetV3 Full Video Processing Test")
    print("=" * 60)
    
    detections = process_video()
    
    print("\n" + "=" * 60)
    print("Done!")
    print("=" * 60)