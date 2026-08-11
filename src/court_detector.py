import cv2
import numpy as np
from scipy.spatial import distance
from typing import Tuple, List, Optional


class CourtDetector:
    def __init__(self, court_width: float = 5.18, court_height: float = 13.40):
        self.court_width = court_width
        self.court_height = court_height
        self.court_corners = None
        self.homography_matrix = None
        self.standard_court_corners = np.array([
            [0, 0],
            [court_width, 0],
            [court_width, court_height],
            [0, court_height]
        ], dtype=np.float32)

    def detect_court(self, frame: np.ndarray) -> Optional[np.ndarray]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blurred, 50, 150)

        lines = cv2.HoughLinesP(
            edges,
            rho=1,
            theta=np.pi / 180,
            threshold=100,
            minLineLength=100,
            maxLineGap=20
        )

        if lines is None:
            return None

        horizontal_lines = []
        vertical_lines = []

        for line in lines:
            x1, y1, x2, y2 = line[0]
            dx = x2 - x1
            dy = y2 - y1
            angle = np.arctan2(dy, dx) * 180 / np.pi

            if abs(angle) < 15 or abs(angle - 180) < 15:
                horizontal_lines.append(line[0])
            elif abs(angle - 90) < 15 or abs(angle + 90) < 15:
                vertical_lines.append(line[0])

        if len(horizontal_lines) < 2 or len(vertical_lines) < 2:
            return None

        corners = self._find_corners(horizontal_lines, vertical_lines)
        if corners is None:
            return None

        corners = self._sort_corners(corners)
        self.court_corners = corners

        self.homography_matrix, _ = cv2.findHomography(
            corners.astype(np.float32),
            self.standard_court_corners
        )

        return corners

    def _find_corners(self, horizontal_lines: List[np.ndarray], vertical_lines: List[np.ndarray]) -> Optional[np.ndarray]:
        intersections = []
        for h_line in horizontal_lines:
            for v_line in vertical_lines:
                x1, y1, x2, y2 = h_line
                x3, y3, x4, y4 = v_line

                denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
                if denom == 0:
                    continue

                t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / denom
                u = -((x1 - x2) * (y1 - y3) - (y1 - y2) * (x1 - x3)) / denom

                if 0 <= t <= 1 and 0 <= u <= 1:
                    x = x1 + t * (x2 - x1)
                    y = y1 + t * (y2 - y1)
                    intersections.append((x, y))

        if len(intersections) < 4:
            return None

        intersections = np.array(intersections)
        unique_corners = self._remove_close_points(intersections)

        if len(unique_corners) < 4:
            return None

        return unique_corners[:4]

    def _remove_close_points(self, points: np.ndarray, threshold: float = 50) -> np.ndarray:
        result = []
        for i, point in enumerate(points):
            is_close = False
            for j, existing_point in enumerate(result):
                if distance.euclidean(point, existing_point) < threshold:
                    is_close = True
                    break
            if not is_close:
                result.append(point)
        return np.array(result)

    def _sort_corners(self, corners: np.ndarray) -> np.ndarray:
        center = np.mean(corners, axis=0)
        angles = np.arctan2(corners[:, 1] - center[1], corners[:, 0] - center[0])
        sorted_indices = np.argsort(angles)
        return corners[sorted_indices]

    def pixel_to_court_coords(self, pixel_x: float, pixel_y: float) -> Tuple[float, float]:
        if self.homography_matrix is None:
            return pixel_x, pixel_y

        pixel_point = np.array([[pixel_x, pixel_y, 1]], dtype=np.float32)
        court_point = cv2.perspectiveTransform(pixel_point[np.newaxis, :, :], self.homography_matrix)
        return float(court_point[0, 0, 0]), float(court_point[0, 0, 1])

    def is_inside_court(self, pixel_x: float, pixel_y: float, margin: float = 0.2) -> bool:
        court_x, court_y = self.pixel_to_court_coords(pixel_x, pixel_y)
        return (-margin <= court_x <= self.court_width + margin and
                -margin <= court_y <= self.court_height + margin)

    def draw_court(self, frame: np.ndarray) -> np.ndarray:
        if self.court_corners is None:
            return frame

        overlay = frame.copy()
        cv2.polylines(overlay, [self.court_corners.astype(np.int32)], True, (0, 255, 0), 2)

        for i, corner in enumerate(self.court_corners):
            cv2.circle(overlay, (int(corner[0]), int(corner[1])), 5, (0, 0, 255), -1)
            cv2.putText(overlay, str(i), (int(corner[0]) + 10, int(corner[1])),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)

        return cv2.addWeighted(overlay, 0.5, frame, 0.5, 0)