import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path


DEFAULT_RECORDINGS_DIR = Path.home() / "Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
DEFAULT_DB_PATH = DEFAULT_RECORDINGS_DIR / "CloudRecordings.db"
# Prefer env override; default is the Dropbox 录音机 folder.
DEFAULT_TARGET_DIR = Path(
    os.environ.get(
        "VOICE_MEMOS_TARGET_DIR",
        "/Users/x/Library/CloudStorage/Dropbox-Sbbz/dqg苹果/录音机",
    )
)
DEFAULT_STATE_NAME = ".voice_memos_sync_state.json"
DEFAULT_TRASH_DIR_NAME = "回收站"
DEFAULT_RECENTLY_DELETED_DIR_NAME = "最近删除"
INVALID_FILENAME_CHARS = re.compile(r'[/:*?"<>|\\]')
SPACE_RUN = re.compile(r"\s+")
# mtime tolerance when comparing source/destination identity (seconds).
MTIME_TOLERANCE_SEC = 2.0


def is_m4a_name(name: str) -> bool:
    return name.lower().endswith(".m4a")


def sanitize_title(title: str) -> str:
    cleaned = INVALID_FILENAME_CHARS.sub(" ", title.strip())
    cleaned = SPACE_RUN.sub(" ", cleaned).strip().rstrip(".")
    return cleaned or "未命名"


def resolve_under(base_dir: Path, relative_name: str) -> Path:
    """Join and ensure the result stays under base_dir (blocks path traversal)."""
    base_resolved = base_dir.expanduser().resolve()
    candidate = (base_resolved / relative_name).resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError(f"非法路径，已拒绝：{relative_name}") from exc
    return candidate


def load_recordings(db_path: Path) -> list[dict]:
    # uri + mode=ro avoids accidental writes to Apple's Voice Memos DB.
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT
                ZPATH,
                ZUNIQUEID,
                COALESCE(NULLIF(ZENCRYPTEDTITLE, ''), NULLIF(ZCUSTOMLABELFORSORTING, ''), NULLIF(ZCUSTOMLABEL, '')) AS TITLE,
                ZDATE,
                ZDURATION,
                ZEVICTIONDATE
            FROM ZCLOUDRECORDING
            WHERE ZPATH IS NOT NULL
              AND ZPATH LIKE '%.m4a'
            ORDER BY ZDATE, ZPATH
            """
        ).fetchall()
    finally:
        conn.close()

    items = []
    for rel_path, unique_id, title, zdate, duration, eviction_date in rows:
        items.append(
            {
                "source_name": rel_path,
                "unique_id": (unique_id or "").strip(),
                "title": (title or Path(rel_path).stem).strip(),
                "zdate": zdate,
                "duration": duration,
                "is_recently_deleted": eviction_date is not None,
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
        raise ValueError("state items missing or invalid")
    except Exception as exc:
        corrupt_path = state_path.with_name(
            f"{state_path.name}.corrupt-{int(time.time())}"
        )
        try:
            shutil.copy2(state_path, corrupt_path)
            print(
                f"警告：状态文件损坏，已备份为 {corrupt_path}，将以空状态继续：{exc}",
                file=sys.stderr,
            )
        except OSError as copy_exc:
            print(
                f"警告：状态文件损坏且备份失败（{copy_exc}），将以空状态继续：{exc}",
                file=sys.stderr,
            )
    return {"version": 1, "items": {}}


def save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
    temp_path.replace(state_path)


class SyncLockError(RuntimeError):
    """Raised when another sync instance holds the lock."""


class StateLock:
    """Best-effort exclusive lock via O_EXCL lock file (works on Dropbox folders)."""

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._fd = None

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._fd = os.open(
                str(self.lock_path),
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o644,
            )
            os.write(self._fd, f"{os.getpid()}\n{int(time.time())}\n".encode("utf-8"))
        except FileExistsError as exc:
            age = None
            try:
                age = time.time() - self.lock_path.stat().st_mtime
            except OSError:
                pass
            # Stale lock older than 2 hours: steal it.
            if age is not None and age > 7200:
                try:
                    self.lock_path.unlink()
                except OSError:
                    pass
                try:
                    self._fd = os.open(
                        str(self.lock_path),
                        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                        0o644,
                    )
                    os.write(self._fd, f"{os.getpid()}\n{int(time.time())}\n".encode("utf-8"))
                    return
                except FileExistsError as steal_exc:
                    raise SyncLockError(
                        f"另一同步进程正在运行（锁文件：{self.lock_path}）。"
                    ) from steal_exc
            raise SyncLockError(
                f"另一同步进程正在运行（锁文件：{self.lock_path}）。"
                f"若确认无其它实例，可删除该锁文件后重试。"
            ) from exc

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        try:
            if self.lock_path.exists():
                self.lock_path.unlink()
        except OSError:
            pass

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


def build_target_name(
    title: str,
    source_name: str,
    unique_id: str = "",
    used_names: set[str] | None = None,
) -> str:
    if not is_m4a_name(source_name):
        raise ValueError(f"只支持同步 .m4a 文件：{source_name}")
    # Always use basename so ZPATH like "2024/foo.m4a" cannot create nested dirs.
    source_base = Path(source_name).name
    if not is_m4a_name(source_base):
        raise ValueError(f"只支持同步 .m4a 文件：{source_name}")
    # Sanitize residual path separators that might appear in odd basenames.
    source_base = source_base.replace("/", "_").replace("\\", "_")
    title_part = sanitize_title(title)
    candidate = f"{title_part}+{source_base}"
    used = used_names if used_names is not None else set()
    if candidate not in used:
        return candidate

    stem = Path(source_base).stem
    suffix = Path(source_base).suffix or ".m4a"
    unique_id = (unique_id or "").strip()
    if unique_id:
        short = unique_id.replace("/", "_")[:12]
        candidate = f"{title_part}+{stem}_{short}{suffix}"
        if candidate not in used:
            return candidate
    idx = 2
    while True:
        candidate = f"{title_part}+{stem} ({idx}){suffix}"
        if candidate not in used:
            return candidate
        idx += 1


def build_record_key(unique_id: str, source_name: str) -> str:
    unique_id = (unique_id or "").strip()
    return unique_id or source_name


def build_legacy_name_map(recordings: list[dict]) -> dict[str, str]:
    legacy_map = {}
    reserved = set()
    reserved_fold = set()
    for item in recordings:
        if not is_m4a_name(item["source_name"]):
            continue
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


def file_identity(path: Path) -> tuple[int, float] | None:
    try:
        st = path.stat()
        return (int(st.st_size), float(st.st_mtime))
    except OSError:
        return None


def same_recording(src: Path, dst: Path) -> bool:
    """Identity: size must match; mtime within tolerance (copy2 preserves mtime)."""
    src_id = file_identity(src)
    dst_id = file_identity(dst)
    if src_id is None or dst_id is None:
        return False
    src_size, src_mtime = src_id
    dst_size, dst_mtime = dst_id
    if src_size != dst_size:
        return False
    return abs(src_mtime - dst_mtime) <= MTIME_TOLERANCE_SEC


def find_compat_existing_file(
    target_dir: Path,
    title: str,
    source_name: str,
    used_names: set[str],
    source_path: Path,
) -> Path | None:
    if not target_dir.exists():
        return None
    expected_prefix = sanitize_title(title)
    source_stem = Path(source_name).stem
    date_hint = source_stem.split()[0] if source_stem else ""
    source_id = file_identity(source_path)
    if source_id is None:
        return None
    source_size, source_mtime = source_id
    for candidate in target_dir.glob("*.m4a"):
        if not candidate.is_file() or not is_m4a_name(candidate.name):
            continue
        if candidate.name in used_names:
            continue
        if not candidate.stem.startswith(expected_prefix):
            continue
        if date_hint and date_hint not in candidate.stem:
            continue
        cand_id = file_identity(candidate)
        if cand_id is None:
            continue
        cand_size, cand_mtime = cand_id
        if cand_size != source_size:
            continue
        if abs(cand_mtime - source_mtime) > MTIME_TOLERANCE_SEC:
            continue
        return candidate
    return None


def normalize_state_items(raw_state_items: dict) -> dict[str, dict]:
    normalized = {}
    for source_name, item in raw_state_items.items():
        if not isinstance(item, dict):
            continue
        effective_source_name = str(item.get("source_name") or source_name)
        target_name = str(item.get("target_name") or "")
        if not is_m4a_name(effective_source_name) or not is_m4a_name(target_name):
            continue
        unique_id = str(item.get("unique_id") or "").strip()
        record_key = build_record_key(unique_id, effective_source_name)
        normalized[record_key] = {
            **item,
            "source_name": effective_source_name,
            "target_name": target_name,
            "unique_id": unique_id,
        }
    return normalized


def build_source_name_index(state_items: dict[str, dict]) -> dict[str, str]:
    index = {}
    for record_key, item in state_items.items():
        source_name = str(item.get("source_name") or "")
        if source_name and source_name not in index:
            index[source_name] = record_key
    return index


def move_to_trash(file_path: Path, trash_dir: Path) -> Path | None:
    if not file_path.exists():
        return None
    trash_dir.mkdir(parents=True, exist_ok=True)
    destination = trash_dir / file_path.name
    if destination.exists():
        stem = file_path.stem
        suffix = file_path.suffix
        idx = 2
        while True:
            candidate = trash_dir / f"{stem} ({idx}){suffix}"
            if not candidate.exists():
                destination = candidate
                break
            idx += 1
    file_path.rename(destination)
    return destination


def safe_replace_move(src: Path, dst: Path, trash_dir: Path) -> str:
    """
    Move src -> dst. If dst exists and is a different file, trash dst first.
    Returns: 'renamed' | 'trashed_conflict' | 'noop'
    """
    if src.resolve() == dst.resolve():
        return "noop"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        # Same inode/content path already handled by resolve; trash conflict.
        move_to_trash(dst, trash_dir)
        src.rename(dst)
        return "trashed_conflict"
    src.rename(dst)
    return "renamed"


def atomic_copy2(src: Path, dst: Path) -> None:
    """Copy to a sibling temp file then replace, so partial downloads are not left as final names."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dst.with_name(f".{dst.name}.partial-{os.getpid()}")
    try:
        shutil.copy2(src, temp_path)
        temp_path.replace(dst)
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def lookup_prev_state(
    state_items: dict[str, dict],
    source_name_index: dict[str, str],
    record_key: str,
    source_name: str,
    unique_id: str,
) -> dict:
    prev = state_items.get(record_key)
    if prev is None:
        fallback_key = source_name_index.get(source_name)
        if fallback_key:
            prev = state_items.get(fallback_key)
    if prev is None and unique_id:
        for existing_item in state_items.values():
            if str(existing_item.get("unique_id") or "").strip() == unique_id:
                prev = existing_item
                break
    return prev or {}


def locate_managed_target(
    target_dir: Path,
    recently_deleted_dir: Path,
    target_name: str,
) -> Path | None:
    """Prefer active folder over 最近删除 when both exist (avoid orphaning)."""
    if not target_name or not is_m4a_name(target_name):
        return None
    active = target_dir / target_name
    deleted = recently_deleted_dir / target_name
    if active.exists():
        if deleted.exists() and active.resolve() != deleted.resolve():
            # Keep active; drop the stale deleted twin into a conflict name via trash later if needed.
            pass
        return active
    if deleted.exists():
        return deleted
    return None


def sync_once(recordings_dir: Path, db_path: Path, target_dir: Path, state_path: Path) -> dict:
    target_dir.mkdir(parents=True, exist_ok=True)
    state = load_state(state_path)
    raw_state_items = state.setdefault("items", {})
    state_items = normalize_state_items(raw_state_items)
    state["items"] = state_items
    recordings = load_recordings(db_path)
    legacy_name_map = build_legacy_name_map(recordings)
    recently_deleted_dir = target_dir / DEFAULT_RECENTLY_DELETED_DIR_NAME
    trash_dir = target_dir / DEFAULT_TRASH_DIR_NAME
    claimed_existing_names = set()
    source_name_index = build_source_name_index(state_items)
    next_state_items = {}
    seen_record_keys = set()

    copied = 0
    renamed = 0
    skipped = 0
    trashed = 0
    conflicts = 0
    missing_sources = []
    rejected_paths = []

    for item in recordings:
        source_name = item["source_name"]
        if not is_m4a_name(source_name):
            continue
        try:
            source_path = resolve_under(recordings_dir, source_name)
        except ValueError:
            rejected_paths.append(source_name)
            continue

        unique_id = item["unique_id"]
        record_key = build_record_key(unique_id, source_name)
        title = item["title"]
        is_recently_deleted = item["is_recently_deleted"]
        prev = lookup_prev_state(state_items, source_name_index, record_key, source_name, unique_id)

        # Still present in DB but media file missing (iCloud not downloaded, eviction lag, etc.):
        # keep prior state and DO NOT trash. Only DB disappearance should trash.
        if not source_path.exists():
            missing_sources.append(source_name)
            if prev:
                next_state_items[record_key] = {
                    **prev,
                    "source_name": source_name,
                    "unique_id": unique_id,
                    "title": title or prev.get("title"),
                    "zdate": item["zdate"],
                    "duration": item["duration"],
                    "missing_source": True,
                }
                if prev.get("target_name"):
                    claimed_existing_names.add(str(prev["target_name"]))
            seen_record_keys.add(record_key)
            continue

        prev_name = prev.get("target_name")
        # Prefer stable previous target name when still free and m4a.
        if prev_name and is_m4a_name(str(prev_name)) and str(prev_name) not in claimed_existing_names:
            target_name = str(prev_name)
        else:
            target_name = build_target_name(
                title,
                source_name,
                unique_id=unique_id,
                used_names=claimed_existing_names,
            )
        current_dir = recently_deleted_dir if is_recently_deleted else target_dir
        target_path = current_dir / target_name

        # Only rename from prev_name when this record still exclusively owns that name
        # (not already claimed by an earlier record this run). Prevents stealing
        # another recording's file under corrupt/shared state.
        owns_prev_name = (
            bool(prev_name)
            and is_m4a_name(str(prev_name))
            and (
                str(prev_name) == target_name
                or str(prev_name) not in claimed_existing_names
            )
        )
        if owns_prev_name:
            prev_path = locate_managed_target(target_dir, recently_deleted_dir, str(prev_name))
            # If both folders had the same name, remove the stale twin after preferring active.
            twin_active = target_dir / prev_name
            twin_deleted = recently_deleted_dir / prev_name
            if twin_active.exists() and twin_deleted.exists() and twin_active.resolve() != twin_deleted.resolve():
                move_to_trash(twin_deleted, trash_dir)
                conflicts += 1
                prev_path = twin_active

            if prev_path and (prev_name != target_name or prev_path != target_path):
                # Extra safety: if another live state entry still points at prev_name,
                # do not move it under this record.
                other_owners = [
                    key
                    for key, other in state_items.items()
                    if key != record_key
                    and str(other.get("target_name") or "") == str(prev_name)
                    and key not in seen_record_keys
                ]
                if other_owners and str(prev_name) != target_name:
                    # Leave the shared file; this record will copy/create its own target.
                    prev_path = None
                if prev_path is not None:
                    action = safe_replace_move(prev_path, target_path, trash_dir)
                    if action == "renamed":
                        renamed += 1
                    elif action == "trashed_conflict":
                        renamed += 1
                        conflicts += 1

        if not prev_name and not target_path.exists():
            legacy_name = legacy_name_map.get(source_name)
            legacy_path = target_dir / legacy_name if legacy_name else None
            legacy_deleted_path = recently_deleted_dir / legacy_name if legacy_name else None
            compat_path = None
            if legacy_path and legacy_path.exists():
                compat_path = legacy_path
            elif legacy_deleted_path and legacy_deleted_path.exists():
                compat_path = legacy_deleted_path
            else:
                compat_path = find_compat_existing_file(
                    target_dir,
                    title,
                    source_name,
                    claimed_existing_names,
                    source_path,
                )
                if not compat_path:
                    compat_path = find_compat_existing_file(
                        recently_deleted_dir,
                        title,
                        source_name,
                        claimed_existing_names,
                        source_path,
                    )
            if compat_path is not None and compat_path.exists():
                if compat_path != target_path:
                    action = safe_replace_move(compat_path, target_path, trash_dir)
                    if action == "renamed":
                        renamed += 1
                    elif action == "trashed_conflict":
                        renamed += 1
                        conflicts += 1

        # Never overwrite a name already claimed by another record this run.
        if target_name in claimed_existing_names and not (
            prev_name and str(prev_name) == target_name
        ):
            # Re-resolve a free name (should be rare after build_target_name).
            target_name = build_target_name(
                title,
                source_name,
                unique_id=unique_id or record_key,
                used_names=claimed_existing_names,
            )
            target_path = current_dir / target_name

        if not target_path.exists():
            atomic_copy2(source_path, target_path)
            copied += 1
        else:
            if same_recording(source_path, target_path):
                skipped += 1
            else:
                # Different content at this path: if we own it via prev, refresh;
                # otherwise pick a free name to avoid clobbering another recording.
                owns_path = bool(prev_name and str(prev_name) == target_name)
                if owns_path:
                    atomic_copy2(source_path, target_path)
                    copied += 1
                else:
                    alt_name = build_target_name(
                        title,
                        source_name,
                        unique_id=unique_id or record_key,
                        used_names=claimed_existing_names | {target_name},
                    )
                    target_name = alt_name
                    target_path = current_dir / target_name
                    atomic_copy2(source_path, target_path)
                    copied += 1

        claimed_existing_names.add(target_name)
        next_state_items[record_key] = {
            "title": title,
            "target_name": target_name,
            "source_name": source_name,
            "unique_id": unique_id,
            "zdate": item["zdate"],
            "duration": item["duration"],
        }
        seen_record_keys.add(record_key)

    for record_key, item in state_items.items():
        if record_key in seen_record_keys:
            continue
        target_name = str(item.get("target_name") or "")
        if not is_m4a_name(target_name):
            continue
        # Do not trash a file still claimed by a live record this run.
        if target_name in claimed_existing_names:
            continue
        target_path = locate_managed_target(target_dir, recently_deleted_dir, target_name)
        # Clean up orphan twin if both exist.
        twin_active = target_dir / target_name
        twin_deleted = recently_deleted_dir / target_name
        if twin_active.exists() and twin_deleted.exists() and twin_active.resolve() != twin_deleted.resolve():
            move_to_trash(twin_deleted, trash_dir)
            conflicts += 1
            target_path = twin_active

        if target_path:
            if move_to_trash(target_path, trash_dir) is not None:
                trashed += 1

    # Final twin cleanup for all managed active names (covers first-sync / no-prev cases).
    for item in next_state_items.values():
        target_name = str(item.get("target_name") or "")
        if not is_m4a_name(target_name):
            continue
        twin_active = target_dir / target_name
        twin_deleted = recently_deleted_dir / target_name
        if twin_active.exists() and twin_deleted.exists() and twin_active.resolve() != twin_deleted.resolve():
            move_to_trash(twin_deleted, trash_dir)
            conflicts += 1

    state["items"] = next_state_items
    state["last_run_epoch"] = int(time.time())
    save_state(state_path, state)
    return {
        "recordings": len(recordings),
        "copied": copied,
        "renamed": renamed,
        "skipped": skipped,
        "trashed": trashed,
        "conflicts": conflicts,
        "missing_sources": missing_sources,
        "rejected_paths": rejected_paths,
        "state_path": str(state_path),
        "target_dir": str(target_dir),
        "trash_dir": str(trash_dir),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="同步 macOS 语音备忘录录音到指定目录。")
    parser.add_argument("--recordings-dir", type=Path, default=DEFAULT_RECORDINGS_DIR)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--target-dir",
        type=Path,
        default=DEFAULT_TARGET_DIR,
        help="目标目录。可用环境变量 VOICE_MEMOS_TARGET_DIR 覆盖默认值。",
    )
    parser.add_argument("--state-path", type=Path, default=None)
    parser.add_argument("--watch", action="store_true", help="持续轮询同步。")
    parser.add_argument("--interval", type=float, default=30.0, help="轮询间隔秒数，默认 30。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state_path = args.state_path or (args.target_dir / DEFAULT_STATE_NAME)
    lock_path = state_path.with_name(state_path.name + ".lock")

    if not args.recordings_dir.exists():
        raise SystemExit(f"录音目录不存在：{args.recordings_dir}")
    if not args.db_path.exists():
        raise SystemExit(f"数据库不存在：{args.db_path}")

    backoff = max(1.0, float(args.interval))
    while True:
        try:
            with StateLock(lock_path):
                result = sync_once(args.recordings_dir, args.db_path, args.target_dir, state_path)
            print(
                json.dumps(
                    {
                        "recordings": result["recordings"],
                        "copied": result["copied"],
                        "renamed": result["renamed"],
                        "skipped": result["skipped"],
                        "trashed": result["trashed"],
                        "conflicts": result["conflicts"],
                        "missing_sources": len(result["missing_sources"]),
                        "rejected_paths": len(result["rejected_paths"]),
                        "target_dir": result["target_dir"],
                        "state_path": result["state_path"],
                    },
                    ensure_ascii=False,
                )
            )
            backoff = max(1.0, float(args.interval))
        except SyncLockError as exc:
            print(
                json.dumps(
                    {"error": str(exc), "backoff_sec": backoff, "kind": "lock"},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            if not args.watch:
                raise SystemExit(str(exc))
            time.sleep(backoff)
            backoff = min(backoff * 2.0, 300.0)
            continue
        except Exception as exc:
            print(
                json.dumps(
                    {"error": str(exc), "backoff_sec": backoff},
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            if not args.watch:
                raise
            time.sleep(backoff)
            backoff = min(backoff * 2.0, 300.0)
            continue
        if not args.watch:
            return 0
        time.sleep(max(1.0, float(args.interval)))


if __name__ == "__main__":
    raise SystemExit(main())
