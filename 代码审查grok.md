# 代码审查报告（Grok · 修复后复审）

**项目路径**：`/Users/x/code/music`  
**审查日期**：2026-07-20  
**代码版本**：`breath_reduce_mac.py` **v59**（约 3834 行）、`sync_voice_memos.py`（约 647 行）  
**对照基线**：2026-07-19 初审 → 大规模修复 → 本复审（工作区 post-fix 状态）  
**审查范围**：主应用 DSP/GUI、同步 CLI、回归测试、readme / requirements / rebuild / gitignore  
**方法**：源码静态核验 + 关键路径动态复现 + `test_regressions.py` / `test_map_bug.py` 实测  
**判定口径**：仅列 **CONFIRMED** 与 **PLAUSIBLE** 为开放问题；已修复项不重复列为缺陷；`DEFAULT_TARGET_DIR` 个人 Dropbox 路径为 intentional 产品默认

---

## 1. 执行摘要（含与上次审查对比：已修复 / 仍开放 / 新发现）

相对 **2026-07-19** 初审与上一轮「修复后」笔记，当前树已越过 **数据丢失 / 渲染时长错误** 的 P0 危机。`sync_voice_memos.py` 在缺失源、冲突回收站、原子拷贝、只读 DB、锁与 watch 退避方面达到可靠个人工具水准；`breath_reduce_mac.py` 已落地非重叠渲染、半速子区间切分、后台线程 + 家族内 token、导出 partial、busy 门禁扩展与 `segments_dirty`。

**总体评价（本复审）**：日用音频安全与同步数据安全 **基线已具备**。剩余高严重度问题集中在 **异步 token 命名空间隔离不全**、**busy 时选文件路径/缓冲不同步**、以及 **同步目标名非唯一导致误跳过/误 trash**。半速邻接合并丢失、basename 嵌套、busy 未挡 plot press 等 **上一轮开放项多数已修复或被证伪**。

| 维度 | 2026-07-19 初审 / 早期复审 | 本复审（v59） |
|------|---------------------------|---------------|
| Critical 开放项 | 重叠拉长 / 同步裸 unlink 等 | **0** |
| 渲染正确性 | 重叠可拉长；邻接半速可丢 | **合并非重叠 + 半速子区间切分（回归通过）** |
| 同步数据安全 | 裸删、仅 size、缺失源误 trash | **显著加固**；残留目标名碰撞与 trash 认领语义 |
| UI 并发 | 主线程阻塞 | **后台 + 家族内 token**；**跨 op 不互消（新 High）** |
| busy 交互 | 波形可乱点 | **press/motion/多数按钮已挡**；**select_file 仍可改路径（新 High）** |
| 测试 | 几乎无 | **9 项回归 + map 脚本；全绿** |
| 版本 | 报告写 v58 | **源码 `VERSION = 59`** |

### 1.1 已确认修复（相对初审 / 旧开放列表）

| 项 | 验证 |
|----|------|
| 重叠区间不拉长输出 | `test_overlap_render_does_not_lengthen` → `len=5000` |
| 半速精确 / 模糊匹配 | `half=[(1000,2000)]` / `[(999,2001)]` → `len=4500` |
| 邻接合并后半速仍生效 | `_split_segment_by_half_time` + `test_half_time_survives_adjacent_merge` → `len=4500` |
| 半速 normalize 为交集裁剪（不扩成整段） | `_normalize_half_time_ranges` 仅 clip 相交部分 |
| 缺失源不 trash | `test_missing_source_does_not_trash`：`trashed=0`；DB 删除 → `trashed=1` |
| 冲突不裸删 | `safe_replace_move` → 回收站 |
| size+mtime 身份 | `same_recording`（±2s） |
| 状态损坏备份 | `.corrupt-<ts>` |
| `resolve_under` 拦 `..` | `test_resolve_under_blocks_traversal` |
| `build_target_name` 仅 basename | `Title+foo.m4a`，无嵌套目录 |
| 处理/导出/重写后台化 | worker + `after(0)` + 各 token |
| 导出 `.partial` → `replace` | 失败不覆盖成品 |
| ffmpeg stderr 尾 | `_run_ffmpeg` |
| reprocess 确认含 dirty/半速/选区 | `segments_dirty` 等 |
| busy 禁 plot press/motion + 多数控件 | `_set_busy` / `on_plot_press` |
| 播放 temp 清理 / afplay terminate+wait+kill | `owns_temp` |
| 压限控制率降采样 | `LIMITER_CONTROL_RATE_HZ=4000` |
| 参数 clamp | `np.clip` / `max(0)` |
| 工程卫生 | requirements / readme / gitignore / pyinstaller 多路径发现 |
| 回归测试 | `test_regressions.py` 9/9 OK，`VERSION=59` |

### 1.2 本复审仍开放 / 新发现（见 §5）

| 级别 | ID | 摘要 | 相对上次 |
|------|-----|------|----------|
| High | `async-token-isolation` | process/rewrite/export_refresh token 互不取消 | **新**（异步层回归风险） |
| High | `select-file-while-busy` | busy 时选文件改 `input_path` 但不启动处理 | **新** |
| High | `sync-target-name-collision` | 同 title+basename 共享目标名 → 错内容 / 误 trash | **新**（结构性） |
| Medium | `sync-unmanaged-deleted-twin` | 首次/无状态同步不清理「最近删除」孪生副本 | **部分残留** |
| Low | `sync-identity-size-mtime-only` | 同 size+mtime 内容损坏不刷新 | **仍开放**（可接受权衡） |
| Low | `sync-watch-lock-sysexit` | watch 遇锁争用直接退出，不退避 | **新** |
| Low | `sync-stale-lock-toctou` | 陈旧锁 unlink+O_EXCL 竞态 | **仍开放** |
| Low | `breath-ffmpeg-not-bundled` | PyInstaller 不捆绑 ffmpeg | **仍开放** |
| Info | `version-now-59` | 源码 v59，旧文档/笔记写 v58 | 卫生 |

### 1.3 已证伪 / 不再列为开放缺陷

| 旧主张 | 结论 |
|--------|------|
| busy 仍允许全面 plot 编辑/播放 | **REFUTED**：`on_plot_press`/`motion` 与多数按钮已 gate；仅 mid-drag 完成是窄边 |
| 半速 normalize 把局部标记扩成整段 | **REFUTED**：normalize 为 intersection clip |
| 邻接合并丢半速 | **FIXED**：`_split_segment_by_half_time` |
| `build_target_name` 嵌入 `/` 建嵌套目录 | **FIXED**：`Path(source_name).name` |
| 重处理确认漏 resize | **FIXED**：`segments_dirty` |
| Dropbox 默认路径是缺陷 | **REFUTED**：intentional；`VOICE_MEMOS_TARGET_DIR` / `--target-dir` 可覆盖 |

---

## 2. 项目架构与技术栈

### 2.1 组件树

```
music/
├── breath_reduce_mac.py      # v59：检测 + 渲染 + 压限 + Tk GUI（~3834 行）
├── sync_voice_memos.py       # 语音备忘录同步 CLI（~647 行，stdlib）
├── test_map_bug.py           # 半速时间轴映射冒烟
├── test_regressions.py       # 渲染/半速/同步关键回归（9 项）
├── requirements.txt          # numpy/librosa/soundfile/matplotlib
├── readme.md                 # 依赖、运行、打包、同步行为
├── rebuild_and_run.sh        # 杀旧进程 → 清理 → PyInstaller → open .app
├── *.spec
├── .gitignore
└── 代码审查grok.md           # 本报告
```

### 2.2 技术栈

| 层 | 选型 |
|----|------|
| 语言 | Python 3.10+ |
| 音频 | numpy、librosa、soundfile、ffmpeg、afplay（macOS） |
| UI | tkinter + matplotlib |
| 同步 | sqlite3（`file:…?mode=ro`）、pathlib、json 状态 |
| 打包 | PyInstaller（windowed `.app`） |

### 2.3 数据流：吸气弱化工具

```
选文件 → run_process（busy 门禁 + process_token）
  → 后台 process_breath
      → ffmpeg 解码临时 WAV（finally 清理；stderr 可见）
      → 分析 mono + 播放 multichannel
      → 多 mask 气息检测 → 扩边 → 大声短语 tame / headroom
      → 控制率压限（LIMITER_CONTROL_RATE_HZ=4000）
      → _render_output_audio（合并非重叠 + 半速子区间切分）
  → UI _apply_process_result（token 校验）
  → 手改/半速/resize → 快照 segments/half_time/buffers
      → 后台 rewrite（rewrite_token）→ 更新 output_*
  → 导出：写 .{name}.partial → replace → *_v59.mp3
      → 可选后台回读真实 MP3 刷新图谱（export_refresh_token）
```

### 2.4 数据流：语音备忘录同步

```
StateLock(O_EXCL)
  → 只读 SQLite 加载 ZCLOUDRECORDING
  → 每条：resolve_under(源) → build_target_name(title+basename)
      → 活跃 vs 最近删除（eviction）
      → 缺失源且 DB 仍在：保留 prev 状态（missing_source），不 trash
      → rename：safe_replace_move（冲突→回收站）
      → 拷贝：atomic_copy2（.partial-pid → replace）
      → 已存在：same_recording(size+mtime±2s) → skip / 否则覆盖拷贝
  → DB 消失的 state 项 → 回收站
  → 原子写 state JSON
watch：普通 Exception 指数退避（×2，封顶 300s）；SystemExit 直接抛出
```

**默认目标目录**：`/Users/x/Library/CloudStorage/Dropbox-Sbbz/dqg苹果/录音机`（intentional 个人默认；可用 `VOICE_MEMOS_TARGET_DIR` 或 `--target-dir` 覆盖）。**不作为缺陷。**

---

## 3. 模块详解

### 3.1 `breath_reduce_mac.py`（v59，核心 DSP + GUI）

| 区域 | 说明 |
|------|------|
| 版本 | `VERSION = 59`；窗口标题与导出 `{stem}_v59.mp3` |
| 检测 | 多 mask + `breath_rms_cap` + 相对谷值路径；阈值仍偏硬编码 |
| 渲染 | `_merge_sample_segments` + cursor clamp；`_split_segment_by_half_time` 在合并后按相交子段半速 |
| 半速匹配 | `_is_half_time_sample_segment`：容差 2ms 或任意正重叠（UI 着色可整段紫，音频按子段切） |
| 压限 | `_smooth_gain_attack_release`，控制率约 4 kHz 降采样 |
| 并发 | `process_token` / `rewrite_token` / `export_refresh_token` 三套独立计数 |
| busy | `_set_busy` 禁用 process/export/半速/选区/播放/缩放等；plot press/motion 早退 |
| 重写 | `_compute_rewrite_outputs` 吃 list 快照与 array 引用，适合后台 |
| 导出 | `.partial` → `Path.replace`；可选真实 MP3 图谱刷新 |
| 播放 | afplay；`owns_temp` 清理；stop：terminate + wait(1s) + kill |
| 配置 | `~/Library/Application Support/musicdoubao/config.json` |

**修复引入的正确结构：** 渲染先合并再切半速；rewrite 不读 UI 可变 list（传快照）；导出与解码 temp 在 finally 清理。

### 3.2 `sync_voice_memos.py`

| 能力 | 实现 |
|------|------|
| 路径安全 | `resolve_under` 拒绝 `..` / 逃逸绝对路径 |
| 目标名 | `sanitize_title(title)+{basename(source)}`（防嵌套目录） |
| 身份 | size + mtime（`MTIME_TOLERANCE_SEC=2`） |
| 缺失源 | `missing_source=True`，保留 prev，不 trash |
| 拷贝 | `atomic_copy2`（`.partial-<pid>` + replace） |
| 冲突 | `safe_replace_move` / `move_to_trash` → 回收站（无裸 unlink 目标） |
| 孪生清理 | 有 `prev_name` 或 DB-missing 循环时清理「最近删除」孪生 |
| 并发 | `StateLock` O_EXCL；>2h 陈旧可抢 |
| DB | `file:…?mode=ro` |
| 默认目标 | Dropbox 录音机（intentional）或 env / CLI |
| watch | 普通错误退避；锁争用见 §5 |

### 3.3 测试与工程

| 文件 | 作用 |
|------|------|
| `test_regressions.py` | 重叠渲染、半速精确/模糊/邻接、split、target basename、path traversal、缺失源、冲突进回收站 |
| `test_map_bug.py` | 半速块上 output→source 时间映射（61.4s → ~62.3s） |
| `requirements.txt` | 吸气工具依赖下限 |
| `readme.md` | venv、ffmpeg、同步 watch/env、回收站/身份/锁/corrupt |
| `rebuild_and_run.sh` | PATH → `~/Library/Python/3.14/bin` → `.venv/bin` 找 pyinstaller |
| `.gitignore` | venv、build/dist、`*.log`、state、corrupt |

---

## 4. 优点与已落地的修复

1. **渲染安全网**：`_merge_sample_segments` + cursor clamp，从根上消除双计拉长。  
2. **半速与合并统一模型**：合并后再 `_split_segment_by_half_time`，邻接/内部半速均可缩短（回归覆盖）。  
3. **半速 normalize 正确语义**：与 effective 段 **交集裁剪**，不整段扩张。  
4. **UI 重活出线程**：process / rewrite / export / MP3 回读均 daemon + `after(0)`；同族 token 可取消前序结果。  
5. **导出原子性**：partial 失败不摧毁已有 MP3。  
6. **播放与临时文件纪律**：`owns_temp`、afplay 强停、decode/export WAV finally 清理。  
7. **同步数据安全闭环**：冲突回收站、缺失源保留、原子拷贝、状态损坏备份、只读 DB、单实例锁、watch 异常退避。  
8. **目标名扁平化**：ZPATH 只用 basename，不再建嵌套目录。  
9. **busy / dirty 体验补丁**：plot 新编辑入口门禁；resize 置 `segments_dirty`；重处理 askyesno。  
10. **工程卫生与回归**：requirements/readme/gitignore/rebuild 发现路径；`test_regressions.py` 9/9 通过。  
11. **听感链路完整**：全内存预览、双缓冲（分析/播放）、半速时间轴映射、诊断文本可复制。

---

## 5. 问题清单（仅 CONFIRMED + PLAUSIBLE，按严重级别）

> 仅列 **当前仍成立** 的问题。已修复 / 已证伪项见 §1.1–1.3，不重复开单。  
> 本复审 **PLAUSIBLE 开放项：0**（下列均为 CONFIRMED）。

### [High · CONFIRMED] process / rewrite / export_refresh token 互不取消

- **ID**：`async-token-isolation`  
- **文件**：`/Users/x/code/music/breath_reduce_mac.py`  
- **位置**：`run_process` apply / `_rewrite_output_from_current_segments` apply / `_schedule_actual_output_refresh` apply  
- **是否回归**：是（异步化引入的结构性缺口）  
- **现象与证据**：  
  - 三套独立计数：`process_token`、`rewrite_token`、`export_refresh_token`；启动路径互不 bump。  
  - 同族取消有效（第二次 process 取消第一次 process），**跨族无效**。  
  - `on_plot_release` 在已有 `resize_segment_index` / `pending_resize_index` / `drag_start_sec` 时 **允许在 busy 下完成**；`_apply_process_result` 不清理这些交互字段。  
  - 可复现序列：开始 resize/拖区 → 点重新处理（busy + process worker）→ 松手调度 resize/range → rewrite 快照 **旧** limited 缓冲与段 → process apply 装入新会话 → rewrite apply 仅校验 `rewrite_token` 仍匹配 → **覆盖** `output_audio` / `output_playback_audio` / timeline / display。  
  - 另：导出后的 MP3 图谱刷新不因 reprocess/rewrite 取消，可在新 process 后仍替换 `output_display_audio`（显示层错误）。  
- **影响**：预览/导出可能静默使用上一会话或被 superseded 的编辑缓冲；用户可能导出错误音频。  
- **建议**：统一 generation/op token（或每次 process/rewrite/export-refresh/select_file 启动时 bump 并校验全部）；`_apply_process_result` 清理 resize/drag 状态；busy 时拒绝 plot 完成或排队至 idle。

---

### [High · CONFIRMED] `select_file` 无视 `is_busy`，路径与缓冲可脱节

- **ID**：`select-file-while-busy`  
- **文件**：`/Users/x/code/music/breath_reduce_mac.py`  
- **位置**：`select_file` / `run_process`  
- **是否回归**：是  
- **现象与证据**：  
  - `select_file` 始终写 `self.input_path`、更新 label/status「正在自动处理…」、启用 reprocess，再调 `run_process()`。  
  - **无** `is_busy` 检查；「选取文件」按钮 **不在** `_set_busy` 禁用列表。  
  - `run_process`：`if self.is_busy: return` — **不回滚路径/标签/状态，不取消在飞任务，不 bump token**。  
  - 序列：处理文件 A 时选 B → UI 显示 B +「正在处理」，内存仍是 A；A 完成后仍可 apply（token 仍有效）；`export_output_file` 用 **B 的 stem** 拼输出路径，却写 **A 的缓冲**。  
  - 非 busy 时若在确认对话框取消 reprocess，路径/label 亦已切到新文件而未加载。  
- **影响**：路径标签与音频缓冲不一致；导出文件名与内容可能错配。  
- **建议**：busy 时拦截选文件并提示；或先 cancel（bump 全部 token）再处理新路径。**仅在真正启动 process 后**改 `input_path`/label；`run_process` 早退时回滚 UI。

---

### [High · CONFIRMED] 不同录音可共享同一 `target_name`，导致错内容保留与误 trash

- **ID**：`sync-target-name-collision`  
- **文件**：`/Users/x/code/music/sync_voice_memos.py`  
- **位置**：`build_target_name` / `sync_once` 认领名与 DB-missing trash 循环  
- **是否回归**：否（结构性）  
- **现象与证据**：  
  - `build_target_name` = `f"{sanitize_title(title)}+{Path(source_name).name}"`，**无** per-`record_key` 消歧。  
  - 动态复现：`a/clip.m4a`（u-a, AAAA）与 `b/clip.m4a`（u-b, BBBB），同标题 Meet、等长、mtime 在容差内 → 仅一份 `Meet+clip.m4a`（AAAA）；state 中两 key 都指向该名；二次同步 `skipped=2`（`same_recording` 只比 size+mtime）。  
  - 删除 DB 中 u-a：trash 循环按缺失项 **直接 trash 共享路径**，不检查 `next_state_items` 是否仍有其它 key 认领 → 活跃目录空，u-b 仍在 state；下轮再拷 BBBB。  
- **影响**：静默保留错误内容；删除一条 DB 记录可让仍有效的另一条录音「消失」一整轮同步。  
- **建议**：本轮已占用名则追加 `unique_id` 后缀或 ` (2)`；trash 仅当 **无其它 live state 项仍认领该 target_name**。

---

### [Medium · CONFIRMED] 首次/无状态同步不清理「最近删除」孪生副本

- **ID**：`sync-unmanaged-deleted-twin`  
- **文件**：`/Users/x/code/music/sync_voice_memos.py`  
- **位置**：`sync_once` 孪生清理门控在 `prev_name` / DB-missing 循环  
- **现象与证据**：双目录 twin 清理仅在 (1) `prev_name` 已设、(2) DB-missing 循环。空 state 首次同步：活跃已有正确名则 skip/copy 活跃，**不**移走「最近删除」同名孪生；`conflicts=0`。下一轮有 state 后会清理。纯未管理 orphan（仅在「最近删除」、不在 state/DB）永不触碰。  
- **影响**：「最近删除」堆积陈旧副本；管理路径下一轮可自愈，非永久数据丢失。  
- **建议**：写入/校验 `target_path` 后始终做 twin 检查；文档声明未管理文件不自动 trash。

---

### [Low · CONFIRMED] `same_recording` 无法发现同 size 内容损坏

- **ID**：`sync-identity-size-mtime-only`  
- **文件**：`/Users/x/code/music/sync_voice_memos.py`  
- **位置**：`file_identity` / `same_recording`  
- **现象**：仅 size + mtime±2s。目的地被改成同长度不同字节且 mtime 保持时，永久 skip。  
- **影响**：罕见本地/云损坏或外部编辑不修复（直到 size/mtime 变）。  
- **建议**：维持廉价身份为默认；可选 `--verify`（首尾 N 字节或 hash）。readme 已写 size+mtime，属 intentional 权衡。

---

### [Low · CONFIRMED] watch 模式遇锁争用直接退出，不进入退避

- **ID**：`sync-watch-lock-sysexit`  
- **文件**：`/Users/x/code/music/sync_voice_memos.py`  
- **位置**：`StateLock.acquire` + `main` 的 `except SystemExit: raise`  
- **现象**：非陈旧锁 → `SystemExit`；`SystemExit` ⊄ `Exception`，watch 的 log+指数退避路径不可达。第二实例或残留 `<2h` 的 `.lock` 会永久结束 watcher。  
- **影响**：长跑 `--watch` 需人工删锁/重启；瞬时重叠无自动恢复。  
- **建议**：watch 下将锁失败作软错误（专用 `LockBusy` 或仅在 acquire 外包），log + 既有 backoff；one-shot 与真配置错误仍硬退。

---

### [Low · CONFIRMED] 陈旧锁 steal 为 unlink + O_EXCL（TOCTOU）

- **ID**：`sync-stale-lock-toctou`  
- **文件**：`/Users/x/code/music/sync_voice_memos.py`  
- **位置**：`StateLock.acquire` 陈旧分支  
- **现象**：两进程可同时过期龄检查；交错后可能双持有（后 unlink 掉先创建的路径）或未捕获的 `FileExistsError`。  
- **影响**：需「锁陈旧 >2h + 并发抢」；个人场景概率极低。  
- **建议**：优先 `fcntl.flock`；或写 pid+token 并 re-read 确认所有权，steal 路径捕获 `FileExistsError` 视为争用。

---

### [Low · CONFIRMED] PyInstaller App 仍依赖系统 ffmpeg

- **ID**：`breath-ffmpeg-not-bundled`  
- **文件**：`rebuild_and_run.sh` / `_find_ffmpeg_binary`  
- **现象**：打包无 `--add-binary`；runtime 只查 PATH 与 Homebrew/MacPorts 常见路径；无 `sys._MEIPASS`。spec `binaries=[]`。  
- **影响**：干净 Mac 双击 `.app` 无 ffmpeg 时 decode/export 失败（有 RuntimeError，非静默损坏）。readme 已文档化。  
- **建议**：捆绑静态 ffmpeg，或首次失败弹安装指引（`brew install ffmpeg`）。

---

### [Info · CONFIRMED] VERSION 现为 59（架构笔记曾写 58）

- **ID**：`version-now-59`  
- **文件**：`breath_reduce_mac.py` L18  
- **说明**：导出与窗口标题均为 v59；产品 readme 用 `{版本}` 通配。非功能缺陷。  
- **建议**：审查/笔记以 `VERSION` 为单一真相源（本报告已对齐 59）。

---

### 本复审 PLAUSIBLE

无。先前 PLAUSIBLE 项（busy 全面放行 plot、半速 normalize 扩张、Dropbox 默认路径等）已 REFUTED 或 FIXED。

---

## 6. 音频处理链路

| 阶段 | 状态 | 备注 |
|------|------|------|
| 解码 | 可用 | 依赖系统 ffmpeg；temp WAV finally 清理 |
| 检测 | 可用 | 多 mask + 动态气息上限；阈值丛林，无黄金 IoU 测试 |
| 扩边 / finalize | 可用 | 参数 UI clamp |
| 压限 | 可用 | 控制率 4 kHz；先 limit 再 breath render；rewrite 基于 `limited_*` 一致 |
| 合并 | **正确** | 样本域强制非重叠 |
| 半速 | **正确（相对初审）** | 子区间切分；时域抽半 + fade（非相位声码器，产品定义） |
| 手改 rewrite | 可用 | 快照 + 后台；见 token 跨族风险 §5 |
| 导出 | **安全** | partial→replace；图谱异步刷新需跨 token 取消 |
| 播放 | 可用 | afplay + temp；长段整段落盘属平台约束 |

**专项结论：** 日用「检测 → 预览 → 手改 → 导出」音频正确性已显著好于初审；剩余风险在 **异步状态机** 而非 DSP 数学。

---

## 7. 并发与 UI

| 路径 | 线程模型 | 评价 |
|------|----------|------|
| `process_breath` | 后台 + `process_token` | 同族好；不取消 rewrite/export_refresh |
| rewrite | 后台 + 快照 + `rewrite_token` | 同族好；可在 process 后覆盖新会话 |
| export | 后台 + partial | 好；入口有 `is_busy` |
| MP3 图谱回读 | 后台 + `export_refresh_token` | 显示层；不被 reprocess 取消 |
| plot press/motion | UI | busy 早退 **已修** |
| plot release | UI | **允许完成 mid-drag**（与 token 问题耦合） |
| select_file | UI | **不查 busy**（High） |
| playhead | `after` 节流 | 可接受 |
| afplay | terminate+wait+kill | 已改善 |

**风险浓缩：** token 命名空间孤岛 + 不完整 mutex（busy 挡入口、不挡跨 op 完成与选文件）→ 下一刀应统一 generation 并硬挡/回滚选文件。

---

## 8. 同步可靠性与数据安全

| 场景 | 行为 | 评价 |
|------|------|------|
| 新录音 | `atomic_copy2` | 好 |
| 改标题 | rename；冲突进回收站 | 好 |
| 系统「最近删除」 | `最近删除/` | 好 |
| 源暂缺、DB 仍在 | 保留状态，不 trash | **关键修复，好** |
| DB 消失 | 回收站 | 好（共享 target_name 时见 High） |
| 双目录同名（有 state） | 保活跃、清 twin | 好 |
| 双目录同名（空 state 首次） | 不清理 deleted twin | Medium |
| 状态损坏 | `.corrupt-<ts>` + 空状态 | 好 |
| 多实例 one-shot | StateLock 硬退 | 可接受 |
| 多实例 watch | 锁争用杀 watcher | Low |
| 陈旧锁 | >2h steal，TOCTOU | Low |
| 同 title+basename 不同 ZPATH | 共享文件 + 误 skip/trash | **High** |
| 默认 Dropbox | intentional | 非缺陷 |
| 身份 | size+mtime | 廉价；无内容校验 |

**专项结论：** 普通「一条备忘录 ↔ 一个 basename」工作流已生产可用；**唯一高严重度正确性洞**是目标对象身份（target_name 非唯一 + trash 不按「最后认领者」）。

---

## 9. 可维护性

1. **`breath_reduce_mac.py` ~3.8k 行单体**——DSP/GUI/IO 同文件，修复补丁继续堆叠；建议拆 `detect / render / limit / gui / io`。  
2. **GUI 状态机** 仍多布尔字段（半速/resize/选区/pending）；与 async token 交织，测试成本高。  
3. **检测魔法数** 未全面参数化；调参缺黄金音频 IoU。  
4. **`sync_voice_memos.py` 结构清晰**——小函数、锁、原子 IO、状态机可读，明显好于初审。  
5. **文档** readme 覆盖 venv/ffmpeg/watch/env/trash/identity/lock；版本号以源码 `VERSION` 为准。  
6. **打包** pyinstaller 发现路径改善；ffmpeg 仍外置。

---

## 10. 测试

### 10.1 现状（已跑通）

```text
OK  test_overlap_render_does_not_lengthen
OK  test_half_time_shortens
OK  test_half_time_fuzzy_match
OK  test_half_time_survives_adjacent_merge
OK  test_split_segment_by_half_time
OK  test_build_target_name_no_nested_path
OK  test_resolve_under_blocks_traversal
OK  test_missing_source_does_not_trash
OK  test_conflict_goes_to_trash
All 9 tests passed. VERSION=59

test_map_bug: Output 61.4 mapped to 62.340 (expect 62.3)
```

### 10.2 缺口（建议补）

| 优先级 | 用例 |
|--------|------|
| P0 | 同 title+basename 两 unique_id：拷贝/skip/删一条 DB 后 trash 是否误伤 |
| P0 | 模拟 process apply 后晚到 rewrite apply（token 隔离） |
| P1 | 空 state 下 active+最近删除 twin 清理 |
| P1 | watch 下锁争用应退避而非退出（行为锁定） |
| P2 | same_recording 同 size 不同内容；陈旧锁双抢；ffmpeg 缺失时 GUI 文案 |

---

## 11. 优先修复路线图 P0 / P1 / P2

### P0（正确性 / 错导出 / 错同步）

1. **统一 async generation token**（或启动时交叉 invalidate 三 token）；`_apply_process_result` 清理 resize/drag；busy 完成路径策略明确。  
2. **`select_file` busy 门禁 + 路径回滚**；「选取文件」纳入 `_set_busy` 禁用。  
3. **`build_target_name` 按 record_key 唯一化**；DB-missing trash 仅当无其它 live 认领者。

### P1（体验与同步边角）

4. 首次/无状态同步也清理「最近删除」孪生。  
5. watch 锁争用软失败 + backoff。  
6. App 捆绑 ffmpeg 或首次失败安装指引。  
7. 为 P0 场景补回归测试（碰撞名、token 交叉、select_file busy）。

### P2（工程与加固）

8. 可选 `--verify` 内容指纹。  
9. StateLock 改 flock 或 ownership re-read。  
10. 拆分 breath 单体模块；检测参数 dataclass + 黄金音频。  
11. pytest/CI 固化现有 9 项 + 新用例。

---

## 12. 结论

**修复后复审结论：主数据丢失与渲染时长危机已解除；产品可继续日用。下一刀应砍在「异步 token 统一 + 选文件状态机」与「同步目标名唯一 / trash 认领」上。**

- 初审 Critical / 多数 High（重叠拉长、裸 unlink、缺失源误 trash、主线程阻塞、嵌套目标名、邻接半速丢失、busy 全面放行 plot 等）**已不在开放列表**。  
- `sync_voice_memos.py` 对 **常规单 basename 录音** 达到可靠个人工具水准；**同名碰撞** 是剩余最高正确性风险。  
- `breath_reduce_mac.py` v59 DSP 与导出路径扎实；**异步 UI 层** 仍可能让预览/导出静默错位。  
- 测试从「几乎无」进步到 **可运行的 9 项关键回归**，但尚未覆盖本复审三条 High。  
- **Dropbox 默认目标路径为 intentional**，不是缺陷。

建议在继续功能迭代前至少完成 **§11 P0 三条** 并固化对应自动化用例。

---

### 附录 A：本复审动态 / 回归摘要

```text
overlap render:              5000 → 5000                 OK（曾 Critical）
half fuzzy:                  4500                        OK
half + adjacent merge:       4500                        OK（曾 High 开放）
half interior split:         架构正确                    OK
missing source:              trashed=0                   OK
DB delete:                   trashed=1                   OK
path .. :                    rejected                    OK
target name slash:           Title+foo.m4a               OK（曾 Medium）
target name collision:       共享名 + 误 trash           OPEN High
async token isolation:       三 token 不交叉             OPEN High
select_file while busy:      路径/缓冲脱节               OPEN High
test_regressions.py:         9/9                         OK
VERSION:                     59                          对齐源码
DEFAULT_TARGET_DIR Dropbox:  intentional                 非缺陷
```

### 附录 B：审查元数据

- 对照 2026-07-19 初审与中间修复笔记，对 **当前工作区** 源码逐项核验。  
- 用户将默认同步目录保持/恢复为个人 Dropbox：按 **intentional** 处理。  
- 未将「半速会升调」列为缺陷（产品定义为时间减半，非 time-stretch）。  
- 未将「播放整段落盘临时 WAV」升为开放缺陷（平台 afplay 约束；owns_temp 已管理生命周期）。  
- 置信度：三条 High 均 ≥0.9，并有源码路径或动态复现支撑。

---

*报告生成：2026-07-20 · Grok 修复后复审 · 基于 `/Users/x/code/music` 当前 post-fix 工作区。*
