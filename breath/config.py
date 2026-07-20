"""App config persistence and optional event logging."""
import json
from pathlib import Path

from .constants import APP_CONFIG_PATH, DEFAULT_DETECT_PARAMS

EVENT_LOG_ENABLED = False
_EVENT_LOG_PATH = Path.home() / "breath_event_log.txt"


def event_log(msg: str) -> None:
    """仅在 EVENT_LOG_ENABLED=True 时写日志，不影响性能。"""
    if not EVENT_LOG_ENABLED:
        return
    import datetime
    line = f"[{datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}\n"
    try:
        with _EVENT_LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass


def load_app_config():
    defaults = dict(DEFAULT_DETECT_PARAMS)
    try:
        if APP_CONFIG_PATH.exists():
            with APP_CONFIG_PATH.open("r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                defaults.update(loaded)
    except Exception:
        pass
    return defaults


def save_app_config(config):
    try:
        APP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with APP_CONFIG_PATH.open("w", encoding="utf-8") as fh:
            json.dump(config, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass
