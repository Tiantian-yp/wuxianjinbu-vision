import os
import sys
import cv2
import numpy as np
import torch
from typing import Optional, List, Tuple
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'models', 'TrackNetV3'))

from utils.general import HEIGHT, WIDTH, get_model, generate_frames, to_img


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


@dataclass
class TrackNetV3Detection:
    frame_idx: int
    x: int
    y: int
    visibility: int
    timestamp: float


class TrackNetV3Integrator:
    def __init__(self, tracknet_ckpt: str = None, inpaintnet_ckpt: str = None, device: str = 'auto'):
        self.tracknet = None
        self.inpaintnet = None
        self.device = torch.device('cuda' if torch.cuda.is_available() and device == 'auto' else 'cpu')
        
        self.tracknet_seq_len = 0
        self.bg_mode = ''
        self.img_scaler = (1, 1)
        
        if tracknet_ckpt and os.path.exists(tracknet_ckpt):
            self._load_tracknet(tracknet_ckpt)
    
    def _load_tracknet(self, ckpt_path: str):
        try:
            ckpt = torch.load(ckpt_path, map_location=self.device, weights_only=True)
            self.tracknet_seq_len = ckpt['param_dict']['seq_len']
            self.bg_mode = ckpt['param_dict']['bg_mode']
            self.tracknet = get_model('TrackNet', self.tracknet_seq_len, self.bg_mode)
            self.tracknet.load_state_dict(ckpt['model'])
            self.tracknet.to(self.device)
            self.tracknet.eval()
            print(f"✓ TrackNet loaded: seq_len={self.tracknet_seq_len}, bg_mode={self.bg_mode}, device={self.device}")
        except Exception as e:
            print(f"✗ Failed to load TrackNet: {e}")
    
    def _preprocess_frame(self, frame: np.ndarray) -> np.ndarray:
        frame_rgb = frame[..., ::-1]
        frame_resized = cv2.resize(frame_rgb, (WIDTH, HEIGHT))
        frame_normalized = frame_resized / 255.0
        return frame_normalized
    
    def process_video(self, video_path: str, sample_interval: int = 2) -> List[TrackNetV3Detection]:
        if self.tracknet is None:
            print("TrackNet model not loaded")
            return []
        
        cap = cv2.VideoCapture(video_path)
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.img_scaler = (w / WIDTH, h / HEIGHT)
        
        seq_len = self.tracknet_seq_len
        detections = []
        
        buffer = []
        frame_idx = 0
        
        print(f"Processing video with sample_interval={sample_interval}, total_frames={total_frames}")
        
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break
            
            if frame_idx % sample_interval == 0:
                buffer.append((frame_idx, frame))
            
            if len(buffer) >= seq_len + 1:
                seq_data = buffer[:seq_len + 1]
                buffer = buffer[seq_len:]
                
                seq_frames = [f for _, f in seq_data]
                seq_indices = [idx for idx, _ in seq_data]
                
                seq_processed = np.array([self._preprocess_frame(f) for f in seq_frames])
                seq_processed = np.transpose(seq_processed, (0, 3, 1, 2))
                
                if self.bg_mode == 'concat':
                    x = torch.from_numpy(seq_processed).float().to(self.device)
                    x = x.reshape(1, -1, HEIGHT, WIDTH)
                else:
                    x = torch.from_numpy(seq_processed[:seq_len]).float().to(self.device)
                    x = x.reshape(1, -1, HEIGHT, WIDTH)
                
                with torch.no_grad():
                    y_pred = self.tracknet(x)
                
                for f_idx in range(min(y_pred.shape[1], len(seq_indices))):
                    actual_frame_idx = seq_indices[f_idx]
                    
                    y_p = (y_pred[:, f_idx:f_idx+1] > 0.5).detach().cpu().numpy()
                    heatmap = to_img(y_p[0, 0])
                    bbox_pred = predict_location(heatmap)
                    cx_pred = int(bbox_pred[0] + bbox_pred[2] / 2)
                    cy_pred = int(bbox_pred[1] + bbox_pred[3] / 2)
                    
                    cx_pred = int(cx_pred * self.img_scaler[0])
                    cy_pred = int(cy_pred * self.img_scaler[1])
                    visibility = 0 if cx_pred == 0 and cy_pred == 0 else 1
                    
                    detections.append(TrackNetV3Detection(
                        frame_idx=actual_frame_idx,
                        x=cx_pred,
                        y=cy_pred,
                        visibility=visibility,
                        timestamp=actual_frame_idx / fps
                    ))
            
            frame_idx += 1
            
            if frame_idx % 1000 == 0:
                print(f"  Progress: {frame_idx}/{total_frames} frames ({frame_idx/total_frames*100:.1f}%)")
        
        cap.release()
        
        return detections