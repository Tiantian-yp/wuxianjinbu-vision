import os
import sys
import cv2
import numpy as np
import torch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'models', 'TrackNetV3'))

from utils.general import HEIGHT, WIDTH, get_model, to_img

def predict_location(heatmap):
    if np.amax(heatmap) == 0:
        return 0, 0, 0, 0
    else:
        (cnts, _) = cv2.findContours(heatmap.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        rects = [cv2.boundingRect(ctr) for ctr in cnts]
        max_area_idx = 0
        max_area = rects[0][2] * rects[0][3]
        for i in range(1, len(rects)):
            area = rects[i][2] * rects[i][3]
            if area > max_area:
                max_area_idx = i
                max_area = area
        x, y, w, h = rects[max_area_idx]
        return x, y, w, h

def test_model():
    tracknet_path = './models/TrackNetV3/ckpts/ckpts/TrackNet_best.pt'
    
    print(f"Loading model from: {tracknet_path}")
    
    ckpt = torch.load(tracknet_path, map_location='cpu', weights_only=True)
    seq_len = ckpt['param_dict']['seq_len']
    bg_mode = ckpt['param_dict']['bg_mode']
    
    print(f"seq_len={seq_len}, bg_mode={bg_mode}")
    
    tracknet = get_model('TrackNet', seq_len, bg_mode)
    tracknet.load_state_dict(ckpt['model'])
    tracknet.eval()
    
    print("Model loaded successfully")
    
    test_input_shape = tracknet.down_block_1.conv_1.conv.weight.shape[1]
    print(f"Expected input channels: {test_input_shape}")
    
    if bg_mode == 'concat':
        input_channels = (seq_len + 1) * 3
    else:
        input_channels = seq_len * 3
    
    print(f"Calculated input channels: {input_channels}")
    
    test_input = torch.randn(1, input_channels, HEIGHT, WIDTH)
    
    print(f"Testing with input shape: {test_input.shape}")
    
    try:
        with torch.no_grad():
            output = tracknet(test_input)
        print(f"Forward pass successful! Output shape: {output.shape}")
        return True
    except Exception as e:
        print(f"Forward pass failed: {e}")
        return False

def test_video():
    video_path = "./飞书20260714-202307.mp4"
    tracknet_path = './models/TrackNetV3/ckpts/ckpts/TrackNet_best.pt'
    
    ckpt = torch.load(tracknet_path, map_location='cpu', weights_only=True)
    seq_len = ckpt['param_dict']['seq_len']
    bg_mode = ckpt['param_dict']['bg_mode']
    
    tracknet = get_model('TrackNet', seq_len, bg_mode)
    tracknet.load_state_dict(ckpt['model'])
    tracknet.eval()
    
    cap = cv2.VideoCapture(video_path)
    frame_list = []
    count = 0
    while cap.isOpened() and count < seq_len + 1:
        ret, frame = cap.read()
        if not ret:
            break
        frame_list.append(frame)
        count += 1
    cap.release()
    
    print(f"Loaded {len(frame_list)} frames")
    
    if len(frame_list) < seq_len + 1:
        print("Not enough frames")
        return
    
    seq_processed = []
    for frame in frame_list:
        frame_rgb = frame[..., ::-1]
        frame_resized = cv2.resize(frame_rgb, (WIDTH, HEIGHT))
        frame_normalized = frame_resized / 255.0
        seq_processed.append(frame_normalized)
    
    seq_processed = np.array(seq_processed)
    seq_processed = np.transpose(seq_processed, (0, 3, 1, 2))
    
    print(f"Processed shape: {seq_processed.shape}")
    
    if bg_mode == 'concat':
        x = torch.from_numpy(seq_processed).float()
        x = x.reshape(1, -1, HEIGHT, WIDTH)
    else:
        x = torch.from_numpy(seq_processed[:seq_len]).float()
        x = x.reshape(1, -1, HEIGHT, WIDTH)
    
    print(f"Input shape: {x.shape}")
    
    try:
        with torch.no_grad():
            y_pred = tracknet(x)
        print(f"Forward pass successful! Output shape: {y_pred.shape}")
        
        for f_idx in range(y_pred.shape[1]):
            y_p = (y_pred[:, f_idx:f_idx+1] > 0.5).detach().cpu().numpy()
            heatmap = to_img(y_p[0, 0])
            bbox_pred = predict_location(heatmap)
            cx_pred = int(bbox_pred[0] + bbox_pred[2] / 2)
            cy_pred = int(bbox_pred[1] + bbox_pred[3] / 2)
            visibility = 0 if cx_pred == 0 and cy_pred == 0 else 1
            print(f"Frame {f_idx}: ({cx_pred}, {cy_pred}), visibility={visibility}")
        
        return True
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"Forward pass failed: {e}")
        return False

if __name__ == '__main__':
    print("=" * 60)
    print("TrackNetV3 Integration Test")
    print("=" * 60)
    
    print("\n--- Test 1: Model Loading ---")
    success1 = test_model()
    
    print("\n--- Test 2: Video Processing ---")
    success2 = test_video()
    
    print("\n" + "=" * 60)
    print(f"Test Results: {'PASSED' if success1 and success2 else 'FAILED'}")
    print("=" * 60)