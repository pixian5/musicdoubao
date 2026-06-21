# music

## macOS 语音备忘录同步

项目内已有同步脚本：

```bash
.venv/bin/python sync_voice_memos.py
```

持续轮询同步：

```bash
.venv/bin/python sync_voice_memos.py --watch --interval 30
```

默认读取 macOS 语音备忘录目录：

```text
~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings
```

默认同步到 Dropbox 目录：

```text
/Users/x/Library/CloudStorage/Dropbox-Sbbz/dqg苹果/录音机
```

同步状态文件默认保存在目标目录：

```text
/Users/x/Library/CloudStorage/Dropbox-Sbbz/dqg苹果/录音机/.voice_memos_sync_state.json
```

如果需要指定目录，可以使用：

```bash
.venv/bin/python sync_voice_memos.py \
  --recordings-dir "/path/to/Recordings" \
  --db-path "/path/to/Recordings/CloudRecordings.db" \
  --target-dir "/path/to/target"
```
