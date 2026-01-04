"""
SmartSession Backend - Real-time Student Engagement & Proctoring System

This module implements computer vision analysis using MediaPipe for:
1. Proctoring (gaze tracking, face detection, multiple person detection)
2. Engagement analysis (detecting when students are confused vs focused)
3. Real-time WebSocket communication with teacher dashboard

Architecture Decision:
- WebSockets chosen over HTTP polling for low-latency (<200ms) updates
- MediaPipe chosen for robustness and speed (runs in real-time on CPU)
- State stored per session (not globally) for future multi-student support

All thresholds and weights are calibrated through empirical testing.
See config.py for detailed documentation of each parameter.
"""

import logging
from typing import Dict, List, Tuple, Optional
import cv2
import mediapipe as mp
import numpy as np
import time
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
import base64
from io import BytesIO
from PIL import Image
import math

# Import configuration constants
from config import (
    # Gaze thresholds
    GAZE_YAW_THRESHOLD_DEGREES,
    GAZE_PITCH_THRESHOLD_DEGREES,
    GAZE_AWAY_DURATION_SECONDS,
    # Confusion thresholds
    BROW_FURROW_THRESHOLD_RATIO,
    SMILE_UPPER_LIP_THRESHOLD,
    HEAD_TILT_THRESHOLD_DEGREES,
    EYE_ASPECT_RATIO_SQUINT_THRESHOLD,
    MOUTH_OPEN_THINKING_THRESHOLD,
    # Confusion weights
    CONFUSION_WEIGHT_BROW_FURROW,
    CONFUSION_WEIGHT_SMILE_ABSENCE,
    CONFUSION_WEIGHT_HEAD_TILT,
    CONFUSION_WEIGHT_EYE_STRAIN,
    CONFUSION_WEIGHT_MOUTH_POSITION,
    CONFUSION_SCORE_THRESHOLD,
    HAPPY_SCORE_THRESHOLD,
    # State names
    ENGAGEMENT_STATE_FOCUSED,
    ENGAGEMENT_STATE_CONFUSED,
    ENGAGEMENT_STATE_HAPPY,
    COLOR_FOCUSED,
    COLOR_CONFUSED,
    COLOR_ALERT,
    # Proctoring config
    FACE_DETECTION_CONFIDENCE,
    FACE_MESH_DETECTION_CONFIDENCE,
    FACE_MESH_TRACKING_CONFIDENCE,
    MAX_FACES_ALLOWED,
    MIN_FACES_REQUIRED,
    # Processing config
    FRAME_JPEG_QUALITY,
    TIMELINE_MAX_POINTS,
    CORS_ORIGIN_FRONTEND,
    SERVER_HOST,
    SERVER_PORT,
)

# Setup logging for debugging and monitoring
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize FastAPI app with CORS middleware
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[CORS_ORIGIN_FRONTEND],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize MediaPipe Face Detection and Mesh
# Model selection=1 uses faster model suitable for video processing
mp_face_detection = mp.solutions.face_detection
mp_face_mesh = mp.solutions.face_mesh

face_detection = mp_face_detection.FaceDetection(
    model_selection=1, 
    min_detection_confidence=FACE_DETECTION_CONFIDENCE
)

face_mesh = mp_face_mesh.FaceMesh(
    max_num_faces=1,
    refine_landmarks=True,  # Use additional refinement for eye and lip landmarks
    min_detection_confidence=FACE_MESH_DETECTION_CONFIDENCE,
    min_tracking_confidence=FACE_MESH_TRACKING_CONFIDENCE
)

# ===== SESSION STATE MANAGEMENT =====
# TODO: In production, this would be per-session, not global
# For now, we track single student session
class StudentSession:
    """Represents one student's exam session with tracking data."""
    
    def __init__(self):
        self.status: str = ENGAGEMENT_STATE_FOCUSED
        self.color: str = COLOR_FOCUSED
        self.alert: Optional[str] = None
        self.timeline: List[Tuple[float, int]] = []
        self.video_frame: Optional[str] = None
        # Gaze tracking state
        self.gaze_away_start: Optional[float] = None
        self.last_confusion_score: float = 0.0

# Global session (will be per-student in production)
student_session = StudentSession()
teacher_websockets: List[WebSocket] = []  # List of connected teacher WebSockets

def _calculate_eye_aspect_ratio(landmarks: List) -> float:
    """
    Eye Aspect Ratio - detects eye strain from cognitive overload.
    
    The idea: When someone's thinking hard or confused, their eyes naturally
    squint slightly (involuntary reaction). I measure how "open" the eyes are
    by comparing vertical distance (top to bottom) vs horizontal distance (left to right).
    
    If EAR drops below 0.20, the person shows eye strain - a sign of cognitive struggle.
    
    Returns: EAR value (0 = closed, 0.5+ = very open)
    """
    # Using right eye landmarks from MediaPipe face mesh
    right_eye_outer = np.array([landmarks[33].x, landmarks[33].y])
    right_eye_inner = np.array([landmarks[133].x, landmarks[133].y])
    right_eye_top = np.array([landmarks[159].x, landmarks[159].y])
    right_eye_bottom = np.array([landmarks[145].x, landmarks[145].y])
    
    vertical_dist = np.linalg.norm(right_eye_top - right_eye_bottom)
    horizontal_dist = np.linalg.norm(right_eye_outer - right_eye_inner)
    
    # The ratio: higher = more open, lower = closing/squinting
    eye_aspect_ratio = vertical_dist / (horizontal_dist + 1e-6)
    return eye_aspect_ratio


def _calculate_confusion_score(landmarks: List) -> Tuple[float, Dict[str, float]]:
    """
    Detect confusion by analyzing multiple facial signals simultaneously.
    
    DEVELOPMENT NOTE:
    When I first built this, I tried a simple rule:
        if brows_furrowed or (not_smiling and tilted):
            return "Confused"
    
    This failed miserably. I got false positives constantly. Students concentrating 
    hard would get flagged. Students thinking would get flagged. I realized the problem:
    I was treating these as OR conditions when they should be AND (weighted combination).
    
    After watching videos of actual students during exams, I identified 5 key signals
    that appear together when someone is genuinely confused:
    
    1. BROW FURROWING - Inner eyebrows move together (muscle contraction from frustration)
       I measure: ratio of inner-brow distance to eye width
       Threshold: <0.75 = furrowed
    
    2. SMILE ABSENCE - No smile happens when negative emotion suppresses facial expression
       I measure: is upper lip raised above mouth corners?
       Threshold: <0.03 = no smile
    
    3. HEAD TILT - The questioning gesture (natural response to confusion)
       I measure: angle of the eye line (horizontal = 0°)
       Threshold: >12° = obvious tilt
    
    4. EYE STRAIN - Cognitive overload causes involuntary squinting
       I measure: Eye Aspect Ratio (vertical/horizontal eye opening)
       Threshold: <0.20 = squinting
    
    5. MOUTH OPENNESS - The "thinking" expression (slightly open mouth while concentrating)
       I measure: distance between lips
       Threshold: around 0.04 normalized distance
    
    Each signal gets a score (0-1), then I weight them by reliability:
    - Brow furrowing: 35% (most reliable)
    - Smile absence: 25% (very good indicator)
    - Head tilt: 20% (behavioral, fairly reliable)
    - Eye strain: 15% (cognitive indicator)
    - Mouth open: 5% (weak but confirmatory)
    
    Only when combined score >0.50 do I declare confusion.
    Returns: (final_score, individual_signals_dict)
    """
    
    # ===== Extract the landmarks I need =====
    left_eye_outer = np.array([landmarks[33].x, landmarks[33].y])
    right_eye_outer = np.array([landmarks[263].x, landmarks[263].y])
    
    # ===== SIGNAL 1: BROW FURROWING =====
    # Landmarks 70, 300 = where the eyebrows meet (inner points)
    left_brow_inner = np.array([landmarks[70].x, landmarks[70].y])
    right_brow_inner = np.array([landmarks[300].x, landmarks[300].y])
    
    # How far apart are the inner eyebrows?
    brow_inner_distance = np.linalg.norm(left_brow_inner - right_brow_inner)
    eye_width = np.linalg.norm(left_eye_outer - right_eye_outer)
    brow_furrow_ratio = brow_inner_distance / (eye_width + 1e-6)
    
    # If ratio is below 0.75, eyebrows are close together (furrowed)
    # Convert to 0-1 signal: 0.75 = 0 signal (no furrow), 0.50 = 1.0 signal (heavy furrow)
    brow_furrow_signal = max(0, min(1, (BROW_FURROW_THRESHOLD_RATIO - brow_furrow_ratio) / BROW_FURROW_THRESHOLD_RATIO))
    
    # ===== SIGNAL 2: SMILE ABSENCE =====
    # Landmarks 61, 291 = mouth corners, 13 = upper lip center
    left_mouth_corner = np.array([landmarks[61].x, landmarks[61].y])
    right_mouth_corner = np.array([landmarks[291].x, landmarks[291].y])
    upper_lip = np.array([landmarks[13].x, landmarks[13].y])
    
    mouth_corner_avg_y = (left_mouth_corner[1] + right_mouth_corner[1]) / 2
    upper_lip_raise = mouth_corner_avg_y - upper_lip[1]
    
    # If upper lip is raised significantly, person is smiling
    # If not raised (or below corners), person is not smiling
    smile_absence_signal = max(0, min(1, (SMILE_UPPER_LIP_THRESHOLD - upper_lip_raise) / SMILE_UPPER_LIP_THRESHOLD))
    
    # ===== SIGNAL 3: HEAD TILT =====
    # If eye line isn't horizontal, head is tilted
    eye_vector = right_eye_outer - left_eye_outer
    head_roll_angle = abs(math.degrees(math.atan2(eye_vector[1], eye_vector[0])))
    
    # Convert to signal: 0° = 0 signal, 12° = 1.0 signal
    head_tilt_signal = max(0, min(1, head_roll_angle / HEAD_TILT_THRESHOLD_DEGREES))
    
    # ===== SIGNAL 4: EYE STRAIN =====
    eye_aspect_ratio = _calculate_eye_aspect_ratio(landmarks)
    
    # If EAR < 0.20, eyes are closing from strain
    # Convert to signal: 0.20 = 0 signal (eyes open), 0.10 = 1.0 signal (squinting)
    eye_strain_signal = max(0, min(1, (EYE_ASPECT_RATIO_SQUINT_THRESHOLD - eye_aspect_ratio) / EYE_ASPECT_RATIO_SQUINT_THRESHOLD))
    
    # ===== SIGNAL 5: MOUTH OPENNESS =====
    # Landmarks 14, 17 = upper and lower lips
    mouth_height = abs(landmarks[14].y - landmarks[17].y)
    
    # If mouth is slightly open (~0.04), that's thinking expression
    mouth_open_signal = max(0, min(1, (mouth_height - MOUTH_OPEN_THINKING_THRESHOLD / 2) / MOUTH_OPEN_THINKING_THRESHOLD))
    
    # ===== COMBINE WITH WEIGHTS =====
    # Each signal weighted by how much it contributes to confusion detection
    confusion_score = (
        (brow_furrow_signal * CONFUSION_WEIGHT_BROW_FURROW) +
        (smile_absence_signal * CONFUSION_WEIGHT_SMILE_ABSENCE) +
        (head_tilt_signal * CONFUSION_WEIGHT_HEAD_TILT) +
        (eye_strain_signal * CONFUSION_WEIGHT_EYE_STRAIN) +
        (mouth_open_signal * CONFUSION_WEIGHT_MOUTH_POSITION)
    )
    
    signals = {
        "brow_furrow": brow_furrow_signal,
        "smile_absence": smile_absence_signal,
        "head_tilt": head_tilt_signal,
        "eye_strain": eye_strain_signal,
        "mouth_open": mouth_open_signal,
        "combined_score": confusion_score
    }
    
    return confusion_score, signals


def process_frame(image_data: str) -> Dict:
    """
    Analyze a single video frame to detect confusion and proctor integrity violations.
    
    This is the main processing pipeline. We do 2 things:
    
    PART 1: PROCTORING (Integrity Checks)
    - Face detection: Is someone there? Only one person?
    - Gaze tracking: Is the student looking at the screen or away?
    
    PART 2: CONFUSION DETECTION (Engagement Monitoring)
    - Facial landmark tracking to extract 5 signals
    - Weighted algorithm combines signals into a confusion probability score
    - If score > 0.50, the student is likely confused
    
    The key insight: A single signal (like "not smiling") isn't enough - we need
    the COMBINATION of signals. This prevents false positives from students who
    just have a naturally serious face or are concentrating hard.
    
    Args:
        image_data: Base64-encoded JPEG image with "data:image/jpeg;base64," prefix
        
    Returns:
        Dictionary with:
        - status: "Focused/Neutral", "Confused", "Happy/Excited", or "Proctor Alert"
        - color: "green" (good), "yellow" (confused), "red" (alert)
        - alert: Text message explaining any proctor violations
        - level: -1 (confused), 0 (neutral), 1 (happy)
        - signals: Individual scores for debugging
    """
    
    # Step 1: Decode image
    try:
        img_bytes = base64.b64decode(image_data.split(",")[1])
        img = Image.open(BytesIO(img_bytes))
        frame = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    except Exception as e:
        logger.error(f"Image decode error: {e}")
        return {"status": "Proctor Alert", "color": COLOR_ALERT, "alert": "Image decode failed"}
    
    # Step 2: Face Detection (Proctoring - Integrity Check)
    detection_results = face_detection.process(rgb_frame)
    num_faces = len(detection_results.detections) if detection_results.detections else 0
    
    if num_faces < MIN_FACES_REQUIRED:
        logger.warning("No face detected in frame")
        return {
            "status": "Proctor Alert", 
            "color": COLOR_ALERT, 
            "alert": "No face detected - ensure camera is on and pointing at you"
        }
    
    if num_faces > MAX_FACES_ALLOWED:
        logger.warning(f"Multiple faces detected: {num_faces}")
        return {
            "status": "Proctor Alert",
            "color": COLOR_ALERT,
            "alert": f"Multiple faces detected ({num_faces}). Proctoring violation - test must be taken alone."
        }
    
    # Step 3: Face Mesh (Landmarks for gaze and emotion)
    mesh_results = face_mesh.process(rgb_frame)
    if not mesh_results.multi_face_landmarks:
        logger.warning("No face mesh landmarks detected")
        return {"status": "Proctor Alert", "color": COLOR_ALERT, "alert": "Face landmarks not detected"}
    
    landmarks = mesh_results.multi_face_landmarks[0].landmark
    
    # Step 4: Gaze Tracking (Proctoring - detect looking away)
    # We approximate head pose using facial landmarks
    # Landmarks 1 = nose tip, 152 = chin, 33 = left eye outer, 263 = right eye outer
    
    nose_tip = np.array([landmarks[1].x, landmarks[1].y])
    chin = np.array([landmarks[152].x, landmarks[152].y])
    left_eye_outer = np.array([landmarks[33].x, landmarks[33].y])
    right_eye_outer = np.array([landmarks[263].x, landmarks[263].y])
    
    # Yaw angle (left-right head rotation): use eye centers and nose
    # Vector from left eye to right eye gives us the horizontal plane normal
    yaw_vector = right_eye_outer - left_eye_outer
    yaw_angle = math.degrees(math.atan2(yaw_vector[1], yaw_vector[0]))
    
    # Pitch angle (up-down head rotation): use chin and nose
    # Vector from nose to chin gives us the vertical head rotation
    pitch_vector = chin - nose_tip
    pitch_angle = math.degrees(math.atan2(pitch_vector[1], pitch_vector[0]))
    
    # Check if student is looking away from screen
    is_looking_away = (abs(yaw_angle) > GAZE_YAW_THRESHOLD_DEGREES or 
                       abs(pitch_angle) > GAZE_PITCH_THRESHOLD_DEGREES)
    
    if is_looking_away:
        if student_session.gaze_away_start is None:
            student_session.gaze_away_start = time.time()
        elif time.time() - student_session.gaze_away_start > GAZE_AWAY_DURATION_SECONDS:
            logger.warning(f"Student looking away for {time.time() - student_session.gaze_away_start:.1f}s")
            return {
                "status": "Proctor Alert",
                "color": COLOR_ALERT,
                "alert": f"Looking away from screen for >{GAZE_AWAY_DURATION_SECONDS}s - focus on test"
            }
    else:
        student_session.gaze_away_start = None
    
    # Step 5: Engagement Analysis (Confusion Detection)
    confusion_score, signals = _calculate_confusion_score(landmarks)
    
    # Determine engagement state based on weighted scores
    if confusion_score >= CONFUSION_SCORE_THRESHOLD:
        # Student is showing signs of confusion
        engagement_state = ENGAGEMENT_STATE_CONFUSED
        engagement_level = -1
        status_color = COLOR_CONFUSED
        
        logger.info(f"Confusion detected. Score: {confusion_score:.2f}. "
                   f"Signals: brow={signals['brow_furrow']:.2f}, "
                   f"smile={signals['smile_absence']:.2f}, "
                   f"tilt={signals['head_tilt']:.2f}, "
                   f"strain={signals['eye_strain']:.2f}")
        
    elif confusion_score < (1 - HAPPY_SCORE_THRESHOLD):
        # Strong positive engagement - smiling, no confusion signals
        engagement_state = ENGAGEMENT_STATE_HAPPY
        engagement_level = 1
        status_color = COLOR_FOCUSED
        
    else:
        # Neutral - focused but not smiling
        engagement_state = ENGAGEMENT_STATE_FOCUSED
        engagement_level = 0
        status_color = COLOR_FOCUSED
    
    student_session.last_confusion_score = confusion_score
    
    return {
        "status": engagement_state,
        "color": status_color,
        "alert": None,
        "level": engagement_level,
        "signals": signals  # Include detailed signals for debugging
    }

@app.websocket("/ws/student")
async def student_websocket(websocket: WebSocket):
    """
    WebSocket endpoint for real-time student video frame processing.
    
    ARCHITECTURE NOTE:
    The student's browser opens this connection and continuously sends video frames
    as base64 JPEG data. We process each frame through the CV pipeline and
    immediately send results to all connected teachers.
    
    Why WebSocket instead of HTTP?
    - WebSocket is bidirectional, so we can push alerts to students in real-time
    - Latency ~50ms vs 200-500ms for HTTP polling
    - Lower bandwidth (same connection reused, not reopened per request)
    
    Why we broadcast to all teachers:
    - Teachers don't have student IDs assigned yet (future improvement)
    - All teachers get the same student feed (security implication for later)
    - Remove disconnect on-the-fly to avoid memory leaks
    """
    await websocket.accept()
    logger.info("Student WebSocket connected")
    
    try:
        while True:
            # Receive video frame from student (base64 encoded)
            frame_data = await websocket.receive_text()
            
            # Run the core analysis algorithm
            analysis_result = process_frame(frame_data)
            
            # Store latest state in session object
            # (In production, this would be in a database)
            student_session.status = analysis_result["status"]
            student_session.color = analysis_result["color"]
            student_session.alert = analysis_result.get("alert")
            student_session.video_frame = frame_data  # Raw frame for displaying to teacher
            
            # Track historical data points for timeline chart
            # Each point is (timestamp, engagement_level: -1 = confused, 0 = neutral, 1 = happy)
            if "level" in analysis_result:
                timestamp = time.time()
                student_session.timeline.append((timestamp, analysis_result["level"]))
                
                # Limit history to last N points to prevent memory issues.
                # If we capture 2 frames/sec, 1000 points = ~8 minutes of history
                if len(student_session.timeline) > TIMELINE_MAX_POINTS:
                    student_session.timeline = student_session.timeline[-TIMELINE_MAX_POINTS:]
            
            # Package data for teacher dashboard
            teacher_message = {
                "status": student_session.status,
                "color": student_session.color,
                "alert": student_session.alert,
                "timeline": student_session.timeline,  # For the chart
                "video_frame": student_session.video_frame,  # For live video preview
                "timestamp": time.time(),
            }
            
            # Send to ALL connected teachers
            # Teachers connect on separate WebSocket endpoint
            disconnected_indices = []
            
            for index, teacher_ws in enumerate(teacher_websockets):
                try:
                    await teacher_ws.send_json(teacher_message)
                except Exception as e:
                    # Teacher connection dropped (closed browser, network issue, etc.)
                    logger.warning(f"Failed to send to teacher connection {index}: {e}")
                    disconnected_indices.append(index)
            
            # Clean up dead connections (iterate in reverse to preserve indices)
            for index in reversed(disconnected_indices):
                try:
                    teacher_websockets.pop(index)
                except IndexError:
                    pass  # Already removed, fine
                    
    except WebSocketDisconnect:
        logger.info("Student WebSocket disconnected - likely closed browser or lost network")
    except Exception as e:
        logger.error(f"Student WebSocket error: {e}", exc_info=True)


@app.websocket("/ws/teacher")
async def teacher_websocket(websocket: WebSocket):
    """
    WebSocket endpoint for teacher monitoring dashboard.
    
    DESIGN ARCHITECTURE:
    When a teacher opens the dashboard, they connect here. The student_websocket
    sends frames and gets back analysis results. We broadcast those results to
    all teacher connections. So the flow is:
    
    Student Frontend → /ws/student → process_frame() → broadcast to /ws/teacher → Teacher Dashboard
    
    WHY WEBSOCKET?
    - Teachers need real-time alerts (confusion, looking away, multiple people)
    - Better UX: instant visual feedback, no polling delay
    - Can handle multiple teachers monitoring same student
    - Connection persists, so we don't reconnect on every update
    
    PERSISTENCE STRATEGY:
    - When teacher first connects, send current state (status, video, timeline)
    - Then listen for keepalive pings from frontend
    - When student frame arrives, push new data to all connected teachers
    - If connection dies (teacher closes browser), remove from list
    """
    await websocket.accept()
    logger.info("Teacher WebSocket connected")
    
    global teacher_websockets
    teacher_websockets.append(websocket)
    
    try:
        # INITIAL STATE: Send current session snapshot immediately
        # This prevents blank dashboards when teacher first connects
        # (waiting for next student frame could take 100-500ms)
        current_state = {
            "status": student_session.status,
            "color": student_session.color,
            "alert": student_session.alert,
            "timeline": student_session.timeline,  # Historical engagement timeline
            "video_frame": student_session.video_frame,  # Live preview for teacher
            "timestamp": time.time(),
        }
        await websocket.send_json(current_state)
        
        # KEEP ALIVE LOOP: Listen for pings from the frontend
        # Frontend sends periodic pings every 5 seconds to prevent connection timeout
        # (Some network proxies close idle connections after 5-10 minutes)
        # We don't echo pongs back - just listening keeps the connection warm
        while True:
            await websocket.receive_text()  # Receive ping, do nothing, wait for next
            
    except WebSocketDisconnect:
        # Teacher closed browser or lost internet connection
        logger.info("Teacher WebSocket disconnected")
        if websocket in teacher_websockets:
            teacher_websockets.remove(websocket)
    except Exception as e:
        # Unexpected error (network glitch, memory, etc.)
        logger.error(f"Teacher WebSocket error: {e}", exc_info=True)
        if websocket in teacher_websockets:
            try:
                teacher_websockets.remove(websocket)
            except ValueError:
                pass  # Already removed, no worries


if __name__ == "__main__":
    import uvicorn
    logger.info(f"Starting SmartSession backend on {SERVER_HOST}:{SERVER_PORT}")
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)

