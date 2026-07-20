"""Shared constants for breath reduction pipeline."""
from pathlib import Path

VERSION = 64
HOP_LENGTH = 512
LEFT_APPEND_MS = 20.0
RIGHT_APPEND_MS = 0.0
MIN_MANUAL_DRAG_SEC = 0.03
MIN_RESIZE_DRAG_SEC = 0.02
PLAYHEAD_DRAW_INTERVAL_MS = 120
HALF_TIME_MATCH_TOLERANCE_SEC = 0.002
LIMITER_CONTROL_RATE_HZ = 4000.0
APP_CONFIG_PATH = Path.home() / "Library" / "Application Support" / "musicdoubao" / "config.json"

DEFAULT_DETECT_PARAMS = {
    "atten_db": 30,
    "sensitivity": 10,
    "export_bitrate_kbps": 128,
    "peak_reject": 3.0,
    "percentile_reject": 20.0,
    "voice_floor": 2.0,
    "left_append_ms": LEFT_APPEND_MS,
    "right_append_ms": RIGHT_APPEND_MS,
    "min_segment_length_ms": 0.0,
}
