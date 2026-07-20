# music

macOS 向清唱/录音小工具集：

1. **吸气声弱化工具**（`breath_reduce_mac.py`）— Tk 图形界面，检测并衰减吸气/气息段，支持半速、手改区间、内存预览与导出 MP3  
2. **语音备忘录同步**（`sync_voice_memos.py`）— 将 Apple 语音备忘录 `.m4a` 同步到指定目录（支持最近删除 / 回收站）

## 系统依赖

- Python 3.10+（推荐 venv）
- [ffmpeg](https://ffmpeg.org/)（解码输入、导出 MP3；Homebrew: `brew install ffmpeg`）
- macOS 自带 `afplay`（试听）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 吸气声弱化工具

DSP 已拆到 `breath/` 包（`detect` / `render` / `limit` / `io` / `segments`）；GUI 入口仍是：

```bash
.venv/bin/python breath_reduce_mac.py
```

打包 macOS App：

```bash
./rebuild_and_run.sh
# 或
pyinstaller --noconfirm --windowed --name "吸气声弱化工具" --clean breath_reduce_mac.py
```

说明：

- 处理全程在内存中完成，只有点击 **导出** 才写入 `{文件名}_v{版本}.mp3`
- 需要本机可执行的 `ffmpeg`（常见路径：`/opt/homebrew/bin/ffmpeg`）
- 参数会保存在 `~/Library/Application Support/musicdoubao/config.json`

## macOS 语音备忘录同步

```bash
.venv/bin/python sync_voice_memos.py --target-dir "/path/to/target"
```

持续轮询：

```bash
.venv/bin/python sync_voice_memos.py --watch --interval 30 --target-dir "/path/to/target"
```

默认读取 macOS 语音备忘录目录：

```text
~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings
```

默认目标目录（可用环境变量覆盖）：

```text
$VOICE_MEMOS_TARGET_DIR  或  /Users/x/Library/CloudStorage/Dropbox-Sbbz/dqg苹果/录音机
```

同步状态文件默认保存在目标目录：

```text
<target-dir>/.voice_memos_sync_state.json
```

完整参数示例：

```bash
.venv/bin/python sync_voice_memos.py \
  --recordings-dir "/path/to/Recordings" \
  --db-path "/path/to/Recordings/CloudRecordings.db" \
  --target-dir "/path/to/target"
```

行为摘要：

- 活跃录音 → 目标目录；系统「最近删除」→ `最近删除/`；从 DB 消失 → `回收站/`
- 重命名冲突时会把旧目标移入回收站，**不再直接删除**
- 是否需要更新按 **size + mtime** 判断
- 状态文件损坏会备份为 `.corrupt-<timestamp>` 后以空状态继续
- 使用 `.lock` 文件防止多实例互踩

## 测试

```bash
.venv/bin/python test_map_bug.py
.venv/bin/python test_regressions.py
```

`test_regressions.py` 覆盖：重叠渲染长度、半速（精确/模糊/邻接合并/resize 比例映射）、目标文件名清洗与碰撞消歧、路径穿越、缺失源不误删、冲突进回收站、mono 缓冲共享、twin 清理。
