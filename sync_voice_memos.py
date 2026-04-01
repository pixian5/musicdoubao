import argparse
import json
import re
import shutil
import sqlite3
import time
from pathlib import Path


DEFAULT_RECORDINGS_DIR = Path.home() / "Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
DEFAULT_DB_PATH = DEFAULT_RECORDINGS_DIR / "CloudRecordings.db"
DEFAULT_TARGET_DIR = Path("/Users/x/Library/CloudStorage/Dropbox-Sbbz/dqg苹果/录音机")
DEFAULT_STATE_NAME = ".voice_memos_sync_state.json"
INVALID_FILENAME_CHARS = re.compile(r'[/:*?"<>|\\]')
SPACE_RUN = re.compile(r"\s+")


def sanitize_title(title: str) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub(" ", title.strip())
    cleaned = SPACE_RUN.sub(" ", cleaned).strip().rstrip(".")
    return cleaned or "未命名"


def load_recordings(db_path: Path) -> list[dict]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            """
            SELECT
                ZPATH,
                COALESCE(NULLIF(ZENCRYPTEDTITLE, ''), NULLIF(ZCUSTOMLABELFORSORTING, ''), NULLIF(ZCUSTOMLABEL, '')) AS TITLE,
                ZDATE,
                ZDURATION
            FROM ZCLOUDRECORDING
            WHERE ZPATH IS NOT NULL
              AND ZPATH LIKE '%.m4a'
            ORDER BY ZDATE, ZPATH
            """
        ).fetchall()
    finally:
        conn.close()

    items = []
    for rel_path, title, zdate, duration in rows:
        items.append(
            {
                "source_name": rel_path,
                "title": (title or Path(rel_path).stem).strip(),
                "zdate": zdate,
                "duration": duration,
            }
        )
    return items


def load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {"version": 1, "items": {}}
    try:
        with state_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            return data
    except Exception:
        pass
    return {"version": 1, "items": {}}


def save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
    temp_path.replace(state_path)


def build_target_name(title: str, source_name: str) -> str:
    return f"{sanitize_title(title)}+{source_name}"


def build_legacy_name_map(recordings: list[dict]) -> dict[str, str]:
    legacy_map = {}
    reserved = set()
    reserved_fold = set()
    for item in recordings:
        base = sanitize_title(item["title"])
        candidate = f"{base}.m4a"
        idx = 2
        while True:
            folded = candidate.casefold()
            if candidate not in reserved and folded not in reserved_fold:
                break
            candidate = f"{base} ({idx}).m4a"
            idx += 1
        reserved.add(candidate)
        reserved_fold.add(candidate.casefold())
        legacy_map[item["source_name"]] = candidate
    return legacy_map


def find_compat_existing_file(target_dir: Path, title: str, source_name: str, used_names: set[str], source_size: int) -> Path | None:
    expected_prefix = sanitize_title(title)
    source_stem = Path(source_name).stem
    date_hint = source_stem.split()[0] if source_stem else ""
    for candidate in target_dir.glob("*.m4a"):
        if candidate.name in used_names:
            continue
        if not candidate.stem.startswith(expected_prefix):
            continue
        if date_hint and date_hint not in candidate.stem:
            continue
        try:
            if candidate.stat().st_size != source_size:
                continue
        except OSError:
            continue
        return candidate
    return None


def sync_once(recordings_dir: Path, db_path: Path, target_dir: Path, state_path: Path) -> dict:
    target_dir.mkdir(parents=True, exist_ok=True)
    state = load_state(state_path)
    state_items = state.setdefault("items", {})
    recordings = load_recordings(db_path)
    legacy_name_map = build_legacy_name_map(recordings)
    claimed_existing_names = set()

    copied = 0
    renamed = 0
    skipped = 0
    missing_sources = []

    for item in recordings:
        source_name = item["source_name"]
        title = item["title"]
        source_path = recordings_dir / source_name
        if not source_path.exists():
            missing_sources.append(source_name)
            continue

        target_name = build_target_name(title, source_name)
        target_path = target_dir / target_name
        prev = state_items.get(source_name, {})
        prev_name = prev.get("target_name")
        prev_path = target_dir / prev_name if prev_name else None
        source_size = source_path.stat().st_size

        if prev_name and prev_name != target_name and prev_path and prev_path.exists():
            if target_path.exists():
                target_path.unlink()
            prev_path.rename(target_path)
            renamed += 1
            claimed_existing_names.add(target_name)

        if not prev_name and not target_path.exists():
            legacy_name = legacy_name_map.get(source_name)
            legacy_path = target_dir / legacy_name if legacy_name else None
            compat_path = None
            if legacy_path and legacy_path.exists():
                compat_path = legacy_path
            else:
                compat_path = find_compat_existing_file(
                    target_dir,
                    title,
                    source_name,
                    claimed_existing_names,
                    source_size,
                )
            if compat_path is not None and compat_path.exists():
                if compat_path.name != target_name:
                    if target_path.exists():
                        target_path.unlink()
                    compat_path.rename(target_path)
                    renamed += 1
                claimed_existing_names.add(target_name)

        if not target_path.exists():
            shutil.copy2(source_path, target_path)
            copied += 1
        else:
            src_stat = source_path.stat()
            dst_stat = target_path.stat()
            if src_stat.st_size != dst_stat.st_size:
                shutil.copy2(source_path, target_path)
                copied += 1
            else:
                skipped += 1
        claimed_existing_names.add(target_name)

        state_items[source_name] = {
            "title": title,
            "target_name": target_name,
            "source_name": source_name,
            "zdate": item["zdate"],
            "duration": item["duration"],
        }

    state["last_run_epoch"] = int(time.time())
    save_state(state_path, state)
    return {
        "recordings": len(recordings),
        "copied": copied,
        "renamed": renamed,
        "skipped": skipped,
        "missing_sources": missing_sources,
        "state_path": str(state_path),
        "target_dir": str(target_dir),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="同步 macOS 语音备忘录录音到指定目录。")
    parser.add_argument("--recordings-dir", type=Path, default=DEFAULT_RECORDINGS_DIR)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--target-dir", type=Path, default=DEFAULT_TARGET_DIR)
    parser.add_argument("--state-path", type=Path, default=None)
    parser.add_argument("--watch", action="store_true", help="持续轮询同步。")
    parser.add_argument("--interval", type=float, default=30.0, help="轮询间隔秒数，默认 30。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state_path = args.state_path or (args.target_dir / DEFAULT_STATE_NAME)

    if not args.recordings_dir.exists():
        raise SystemExit(f"录音目录不存在：{args.recordings_dir}")
    if not args.db_path.exists():
        raise SystemExit(f"数据库不存在：{args.db_path}")

    while True:
        result = sync_once(args.recordings_dir, args.db_path, args.target_dir, state_path)
        print(
            json.dumps(
                {
                    "recordings": result["recordings"],
                    "copied": result["copied"],
                    "renamed": result["renamed"],
                    "skipped": result["skipped"],
                    "missing_sources": len(result["missing_sources"]),
                    "target_dir": result["target_dir"],
                    "state_path": result["state_path"],
                },
                ensure_ascii=False,
            )
        )
        if not args.watch:
            return 0
        time.sleep(max(1.0, float(args.interval)))


if __name__ == "__main__":
    raise SystemExit(main())
