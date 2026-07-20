# 代码审查报告（Grok · v60 high）

**审查日期**：2026-07-20  
**审查对象**：持续演进至 **v62** · 当前 `VERSION = 62`  
**范围**：`breath_reduce_mac.py`、`sync_voice_memos.py`、`test_regressions.py` 等  
**方法**：high effort 多角度扫描 + 对抗验证；`test_regressions.py` **14/14** 通过  

---

## 执行摘要

### 审查复核（相对 v60 报告）

| v60 报告主张 | v61/v62 状态 |
|--------------|--------------|
| 选文件失败路径/缓冲不同步 | **已修**（`loaded_input_path` + 失败回滚 + 导出一致性） |
| resize 扩大局部半速 | **已修**（`_map_half_time_on_resize` 比例映射） |
| `after(10)` resize 无 token | **已修**（`captured_token` + busy 检查） |
| export token 失配不清理 busy | **已修**（`_release_busy_if_token`） |
| sync prev_name 抢文件 | **已修**（`owns_prev_name` + other_owners） |
| UI 整段紫 / 音频子段半速 | **有意 UX**，非正确性 bug |
| 双遍 render / mono copy | **v62 已优化**：mono 共享缓冲 + 同缓冲单次 finalize/render |
| async 三处复制 | **v62**：rewrite 已走 `_run_bg_job` |
| twin 清理复制 | **v62**：`cleanup_name_twins` + `same_recording` 复用 |

**当前无 Critical / 无开放 High 正确性项。** 剩余主要是体验与进一步拆模块。

| 级别 | 数量 |
|------|------|
| Critical | 0 |
| High（正确性） | 0 |
| Medium | 少量效率/架构债 |
| Low / 清理 | 若干 |

---

## 问题清单（按严重度）

### [High] 选文件处理失败后路径/缓冲不同步

- **文件**：`breath_reduce_mac.py:2241`（`select_file` / `run_process` 失败路径）  
- **判定**：CONFIRMED  
- **现象**：`select_file` 先写 `input_path` 与标签，再 `run_process`；失败时只设「处理失败」，**不回滚路径、不清空旧缓冲**。  
- **失败场景**：已加载 A → 选 B → ffmpeg 缺失/损坏失败 → 界面显示 B、波形仍是 A → 导出用 B 的文件名 + A 的 `output_playback_audio`。  
- **建议**：失败时回滚 `input_path`/标签到上次成功文件，或清空缓冲并禁用导出；成功前不要启用 export。

### [High] resize 将局部半速扩展为整段

- **文件**：`breath_reduce_mac.py:3530`（`_apply_segment_resize`）  
- **判定**：CONFIRMED  
- **现象**：`was_half_time = _segment_is_half_time(...)` 为 **任意相交**；为真则 `subtract` 整段旧区间再 `merge` 整段新区间。  
- **失败场景**：段 [1,3]s 仅 [1,2]s 半速 → 拖右边界到 3.5s → 半速变成 [1,3.5]s，多削音频。  
- **建议**：resize 时对 half 区间做仿射映射（只移动端点落在 half 上的部分），或 half 存相对段内比例。

### [High] 延迟 `after(10)` resize 无 generation / busy 门禁

- **文件**：`breath_reduce_mac.py:2870-2872`、`_apply_segment_resize`  
- **判定**：CONFIRMED  
- **现象**：`on_plot_release` 用 `root.after(10, do_resize)` 提交边界调整，回调 **不检查** `is_busy` / `op_token`。  
- **失败场景**：拖边界松手 → 10ms 内点「重新处理」完成 → 延迟回调仍用旧 `segment_idx` 改 **新会话** 的 segments，并触发 rewrite。  
- **建议**：捕获 `op_token`；回调开头 `if token != self.op_token or self.is_busy: return`；或去掉 after，直接调用并依赖 rewrite 异步。

### [High] export apply 在 token 失配时不清理 busy

- **文件**：`breath_reduce_mac.py:3310-3311`  
- **判定**：CONFIRMED（代码路径）/ PLAUSIBLE（卡死需配合其它 bump）  
- **现象**：

  ```python
  if token != self.op_token:
      return  # 未 _set_busy(False)
  ```

  导出 worker 可能已写完 MP3；UI 若失配则永不解除 busy。延迟 resize 启动的 rewrite 通常会再清 busy，但任一侧 apply 被丢弃时可能卡住。  
- **建议**：失配分支也 `_set_busy(False)`（或仅当 `self.is_busy and 无更新的 in-flight op`）；所有 apply 统一 finally 语义。

### [Medium] 损坏 state 下 `prev_name` 重命名可能挪走他人文件

- **文件**：`sync_voice_memos.py:517`  
- **判定**：PLAUSIBLE  
- **现象**：即使 `target_name` 已消歧，只要 `prev_name != target_name` 仍 `safe_replace_move(prev_path, target_path)`；若两记录曾共享 `prev_name`，后处理者会把文件挪到自己的新名。  
- **建议**：仅当 `prev_name` 仍由本 `record_key` 独占（或内容 identity 匹配）时才 rename。

### [Info] UI 整段紫色 vs 渲染子段半速（有意设计，非正确性 bug）

- **文件**：`breath_reduce_mac.py:2551` + `_split_segment_by_half_time`  
- **判定**：REFUTED as correctness — intentional UX  
- **说明**：音频按相交子段半速（正确）；UI 对 effective 段做布尔紫色。若要 WYSIWYG，仅改着色为子区间即可。

### [Medium] 双缓冲全长渲染 ×2 + 多轨常驻内存

- **文件**：`breath_reduce_mac.py:1903`、`1681`  
- **判定**：CONFIRMED（效率）  
- **现象**：plot/playback 各跑一遍 render/finalize；mono 仍 `analysis_audio = y_full.copy()`。  
- **建议**：mono 共享缓冲；先算 gain/段计划再应用到多声道。

### [Low] 控制率压限仍 O(N) 插值

- **文件**：`breath_reduce_mac.py:1424`  
- **判定**：CONFIRMED（效率）  
- **建议**：块常量 gain 或降采样显示包络路径与全采样路径分离。

### [Low] 异步 worker 模板三处复制

- **文件**：`breath_reduce_mac.py:2394` 一带  
- **判定**：PLAUSIBLE（简化）  
- **建议**：`_run_bg(job, on_ok, on_err)` 统一 token/busy。

### [Low] 同步 twin 清理 / identity 重复实现

- **文件**：`sync_voice_memos.py:509`、`303`  
- **判定**：PLAUSIBLE（复用）  
- **建议**：`cleanup_twins()`、`find_compat` 调用 `same_recording`。

### [Low] 半速双列表架构债

- **文件**：`breath_reduce_mac.py` 段状态  
- **判定**：PLAUSIBLE（altitude）  
- **建议**：段对象带 `half_ranges` 或 flag，消灭平行列表。

---

## 已验证为稳妥 / 不作为开放缺陷

| 项 | 说明 |
|----|------|
| 重叠渲染 | 回归：5000→5000 |
| 邻接半速 | 回归：4500 |
| 目标名 basename + 碰撞消歧 | e2e 两源同名 → 两文件 |
| 改标题后 prev_name 稳定 | 不同内容保留 |
| 缺失源不 trash | 回归通过 |
| op_token 跨 op | 无独立 process/rewrite/export token |
| busy 选文件 | 按钮禁用 + 提示 |
| CLAUDE.md 约定 | 仓库无适用规则 |

---

## 测试

```bash
.venv/bin/python test_regressions.py
# All 11 tests passed. VERSION=60
```

建议补测：选文件失败回滚、resize 局部半速不扩展、UI 半速子区间着色。

---

## 优先修复

**P0（v61 已落地）**：选文件失败回滚 / resize 半速比例映射 / after(10) token / busy 安全释放 / prev_name 所有权  
**P1**：双缓冲共享、单次段计划（效率）  
**P2**：async helper 去重、sync twin helper、半速 UI 子区间着色（可选 WYSIWYG）  

---

*high effort multi-angle review · 2026-07-20 · v60*
