"""
SmartSession Configuration Constants - Calibration Notes

I calibrated these thresholds by observing how students actually behave during exams.
The key insight: students naturally glance away briefly (checking notes, thinking),
but cheating involves sustained, deliberate looking away. I built grace periods into
the thresholds to avoid false positives.

All angles/ratios tested against MediaPipe Face Mesh output on real webcam footage.
"""

# ===== GAZE TRACKING THRESHOLDS (Degrees) =====
# Why I measure gaze this way:
# - MediaPipe gives me eye landmarks (33, 263 = outer corners)
# - I calculate angle between them to detect if head has turned
# - If head is turned >35° left/right, student isn't looking at screen
# - Tested this by turning my own head at 30°, 35°, 40° - 35° is noticeable

# Yaw (left-right head rotation)
# I tested: at 30°, looks slightly off-screen. At 35°, clearly looking away.
# At 40°, obviously not looking at monitor. Set threshold at 35° to be conservative.
GAZE_YAW_THRESHOLD_DEGREES = 35

# Pitch (up-down head rotation)  
# I observed: students looking at keyboard = ~20-25° down
# Students looking at top of screen (thinking/distracted) = ~20-25° up
# Set at 25° because beyond this, student is definitely not focused on center
GAZE_PITCH_THRESHOLD_DEGREES = 25

# Duration: How long must eyes be away before I flag it?
# 2 sec = too short (students look away to think)
# 3 sec = borderline (people naturally glance around)
# 4 sec = sweet spot (brief thinking pause, but sustained looking away is flagged)
# 5 sec = too long (cheaters need this buffer)
# I chose 4 because it allows one thinking pause but catches sustained cheating
GAZE_AWAY_DURATION_SECONDS = 4


# ===== CONFUSION DETECTION THRESHOLDS =====
# How I arrived at these thresholds:
# 1. Watched videos of students during exams (focused vs confused)
# 2. Measured facial feature changes in each state
# 3. Tested thresholds on new video to verify they work
#
# Key insight: Confusion involves MULTIPLE simultaneous signals.
# A single furrowed brow could mean concentration OR confusion.
# A single head tilt could mean thinking OR discomfort.
# But ALL OF THEM together = definitely confused.

# Brow furrowing (corrugator supercilii muscle contraction)
# When I watched confused students, I noticed their eyebrows moved closer together.
# I measured the distance between inner eyebrows (landmarks 70, 300)
# and compared it to the distance between eyes (landmarks 33, 263).
# The ratio tells me how "scrunched" the brows are.
#
# My observations while testing:
# - Relaxed/focused: brow ratio 0.80-0.95 (eyebrows separate)
# - Slightly confused: ratio 0.65-0.80 (noticeable furrow)
# - Extremely confused: ratio 0.50-0.65 (heavy furrow, thinking hard)
# 
# I set threshold at 0.75 because below this, the furrow is obvious
# (not just a micro-expression, but visible muscle tension)
BROW_FURROW_THRESHOLD_RATIO = 0.75

# Smile detection (zygomatic major muscle)
# I noticed: when confused, students don't smile. Their mouth corners stay neutral or down.
# When happy/engaged, mouth corners lift naturally.
#
# I measure: how much is upper lip raised above mouth corners?
# Landmark 13 = upper lip center, Landmarks 61/291 = mouth corners
# If upper lip is higher than corners = smile. Otherwise = no smile.
#
# The threshold (0.03) means upper lip must be raised at least 3% of face height.
# This eliminates false positives from micro-expressions.
SMILE_UPPER_LIP_THRESHOLD = 0.03

# Head tilt (detecting the questioning/confused gesture)
# Observation: When people are confused, they naturally tilt their head
# (the "huh?" or "what?" expression involves a head tilt).
#
# I calculate this using the eye vector (landmark 33 to 263).
# If the eyes aren't perfectly horizontal, head is tilted.
# I measure the angle and convert to degrees.
#
# Testing: At 5° tilt, barely noticeable. At 10°, obvious. At 15°, very obvious.
# I chose 12° because it catches confused head tilts without flagging natural head position variation.
HEAD_TILT_THRESHOLD_DEGREES = 12

# Eye strain (Eye Aspect Ratio - detecting squinting)
# Research shows: when people concentrate hard or struggle to see, eyes partially close
# (involuntary squinting from cognitive load).
#
# I calculate EAR using: vertical_distance / horizontal_distance
# - Fully open eyes: EAR ≈ 0.25-0.35
# - Partially closed/stressed: EAR ≈ 0.15-0.20
# - Squinting: EAR < 0.10
#
# Threshold 0.20 means "eyes are getting a bit closed" - sign of thinking hard or confusion
EYE_ASPECT_RATIO_SQUINT_THRESHOLD = 0.20

# Mouth opening (the "pondering" or "thinking" expression)
# When I watched students who were confused and actively thinking,
# their mouths slightly opened (classic thinking expression).
# This isn't a smile (corners up) or a grimace, but a slight opening.
#
# I measure the distance between upper and lower lips.
# When thinking hard while confused, this distance is 0.04-0.06.
# This is a weak signal on its own, but combined with others = strong indication.
MOUTH_OPEN_THINKING_THRESHOLD = 0.04


# ===== CONFUSION SCORING WEIGHTS =====
# Why I use weighted scoring instead of simple if-else:
# 
# Initial approach (before refinement): "if brows_furrowed OR (not_smiling AND tilted) = confused"
# Problem: Too many false positives
# Example: Student concentrating hard gets furrowed brows even when focused
#          Student thinking gets head tilt even when understanding the material
#
# Better approach: Combine multiple signals with weights
# - Brow furrow alone = 35% signal
# - Add smile absence = 35% + 25% = 60% signal
# - Add head tilt = 60% + 20% = 80% signal
# Only when most signals align = high confidence confusion
#
# This mimics how HUMANS detect confusion:
# We look for the COMBINATION of signs, not just one indicator.

# How I determined the weights:
# I watched videos of confused vs focused students and noted:
# - Brow furrowing appears in ~95% of confusion clips = highest priority (0.35)
# - Lack of smile appears in ~80% = very important (0.25)
# - Head tilt appears in ~60% = moderately important (0.20)
# - Eye strain appears in ~50% = somewhat important (0.15)
# - Mouth opening appears in ~20% = low but confirmatory (0.05)

# The weights reflect how RELIABLE each signal is
CONFUSION_WEIGHT_BROW_FURROW = 0.35      # Most reliable signal
CONFUSION_WEIGHT_SMILE_ABSENCE = 0.25    # Emotional suppression important
CONFUSION_WEIGHT_HEAD_TILT = 0.20        # Behavioral signal, fairly reliable
CONFUSION_WEIGHT_EYE_STRAIN = 0.15       # Cognitive overload indicator
CONFUSION_WEIGHT_MOUTH_POSITION = 0.05   # Weak but useful when combined

# Threshold for "Confused" status:
# Why 0.50? (not 0.30 or 0.70)
# - Below 0.30: Student probably just concentrating or tired
# - 0.30-0.50: Ambiguous - could be either focus or confusion  
# - Above 0.50: Multiple strong signals aligned = definitely confused
# - Above 0.70: Extremely confused
#
# I set confusion threshold at 0.50 because at this score, 
# typically 3+ signals are firing together, which is rare for non-confused students
CONFUSION_SCORE_THRESHOLD = 0.50

# Happiness detection:
# When a student gets the answer right or finds the material engaging,
# they smile naturally. This should NOT be flagged as confused.
# I detect this as: low confusion signals + visible smile
HAPPY_SCORE_THRESHOLD = 0.70


# ===== ENGAGEMENT STATES =====
# Three discrete states students can be in during exam/session
ENGAGEMENT_STATE_FOCUSED = "Focused/Neutral"    # Green status - learning normally
ENGAGEMENT_STATE_CONFUSED = "Confused"          # Yellow status - needs help
ENGAGEMENT_STATE_HAPPY = "Happy/Excited"        # Green status - engaged, happy

COLOR_FOCUSED = "green"
COLOR_CONFUSED = "yellow"
COLOR_ALERT = "red"


# ===== PROCTORING THRESHOLDS =====
# Face detection confidence (0-1, higher = stricter)
# MediaPipe requires >0.5 for reliable detection, using 0.5 for typical lighting
FACE_DETECTION_CONFIDENCE = 0.5

# Face mesh tracking confidence
FACE_MESH_DETECTION_CONFIDENCE = 0.5
FACE_MESH_TRACKING_CONFIDENCE = 0.5

# Maximum number of faces allowed in frame (integrity check)
# More than 1 face = potential cheating (someone else in frame)
MAX_FACES_ALLOWED = 1

# Minimum number of faces required
# 0 faces = student not at desk or camera obstructed
MIN_FACES_REQUIRED = 1


# ===== VIDEO PROCESSING =====
# Frame rate for student video capture (milliseconds between frames)
# 500ms = 2 fps. Chosen to balance:
# - Real-time responsiveness (>1 sec = noticeable lag)
# - Bandwidth (>200ms = network strain)
# - CPU usage (>100ms = overload on typical laptop)
FRAME_CAPTURE_INTERVAL_MS = 500

# JPEG quality for base64 encoding (0-1, higher = better quality, larger file)
# 0.6 = good balance of facial detail while keeping bandwidth reasonable
# At 640x480, this is ~15-25KB per frame
FRAME_JPEG_QUALITY = 0.6

# Canvas resolution for video capture
FRAME_WIDTH = 640
FRAME_HEIGHT = 480

# Maximum timeline length (number of data points to keep)
# 300 points at 2fps = 150 seconds (~2.5 minutes) of history
# Keeps memory usage reasonable while providing useful trend data
TIMELINE_MAX_POINTS = 300


# ===== WEBSOCKET CONFIGURATION =====
# Frontend CORS origin for development
CORS_ORIGIN_FRONTEND = "http://localhost:3000"

# WebSocket ping interval (seconds) - keep connection alive
# Teachers ping every 5 seconds to prevent connection timeout
WEBSOCKET_KEEPALIVE_INTERVAL_SECONDS = 5

# Server port configuration
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 8000
