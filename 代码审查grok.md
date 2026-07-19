# 代码审查报告（Grok · v60 high）

**审查日期**：2026-07-20  
**审查对象**：`6af53e2`（`085d284...HEAD`）· `VERSION = 60`  
**范围**：`breath_reduce_mac.py`、`sync_voice_memos.py`、`test_regressions.py` 等  
**方法**：high effort 多角度扫描 + 对抗验证；`test_regressions.py` 11/11 通过  

---

## 执行摘要

v60 相对 v57 已修好：重叠渲染、半速邻接合并、主线程阻塞、同步裸删/缺失源误 trash、目标名嵌套、token 互消、busy 门禁等。**当前无 Critical**。  

仍开放的 **CONFIRMED 正确性** 集中在：

1. 选文件失败后 **路径与缓冲不同步**（可导出错误内容）  
2. **resize 会扩大半速覆盖**  

「UI 整段紫色 / 音频子段半速」经对抗验证为 **有意粗粒度着色**，不记作正确性缺陷。其余多为效率与可维护性。

| 级别 | 数量 |
|------|------|
| Critical | 0 |
| High（正确性） | 2 |
| Medium | 1（效率） |
| Low / 清理 | 4+ |

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

**P0**：选文件失败路径/缓冲一致性  
**P0**：resize 半速映射不扩大  
**P1**：半速 UI 与渲染语义统一  
**P2**：双缓冲共享、async helper、sync twin helper  

---

*high effort multi-angle review · 2026-07-20 · v60*
