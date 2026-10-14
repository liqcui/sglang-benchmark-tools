# HiCache L3（Mooncake）测试报告 —— 最优解拓扑下的确定性崩溃复现

**测试时间**：2026-10-05
**测试人**：supperadm（sglbench 驱动，数据落 DuckDB `~/sglang-benchmark-tools/data/bench.duckdb`）
**目的**：在最优解拓扑（pp1/tp8/dp4）+ L2-PP1 补丁镜像基础上进入 L1+L2+L3，验证 L3 写回
在最优拓扑下是否仍触发 MACA 平台崩溃（09-27 定性问题的最终复核）。

---

## 1. 两种 L3 架构（均已部署验证）

| 架构 | 目录 | prefill 镜像 | mooncake | 组件 |
|---|---|---|---|---|
| **Dummy Client**（官方解耦） | `deepseek-hicache-l3/` | `r4-hicache-l2-mc013-pp1`（0.3.13 store + 0.3.6 engine，新建 pp1 补丁镜像） | 0.3.13 store（SHM 解耦） | + client DaemonSet + store-decode pod |
| **简化版**（官方 Quickstart 普通模式） | `deepseek-hicache-l3-simple/` | **`r4-hicache-l2-pp1`**（旧 0.3.6 TE） | 0.3.6（引擎直连 master 自贡献段） | 仅 master（无 client/store-decode） |

两者 prefill 均：**最优解拓扑 pp1/tp8/dp4 + `--enable-dp-attention` + `--hicache-storage-backend=mooncake`**
（launcher 三件套修复：`prefill_pp_size_default` 回写 / dp-attention 注入 / 镜像内 tp 推导补丁）。

启动验证（l3-simple）：master 三端口 LISTEN(50051/8080/9003) + HTTP metadata server ✓；
prefill `Mooncake store setup successfully` + `fired up` ✓；全栈 E2E 200 ✓。
⚠ 关键修复：**master 必须与引擎 client 同版本**（0.3.6）——mc013(0.3.13) master 致
`invalid rpc arg` → `mount_segment_to_master_failed` → `-900`（09-27 同款坑复现）。

## 2. 测试结果：L3 写回在最优拓扑下仍确定性崩溃

**官方案例**（`hicache-multiturn-80x10-r16`，80 客户端×10 轮/2048/out1/rate16/parallel4）：

| 运行 | 架构 | 结果 | 崩溃签名（首崩节点） |
|---|---|---|---|
| `l3on-official` | Dummy Client (0.3.13) | **partial 9.75%** (78/800) | `memory violation(0x4)`（node31）→ gloo 级联 |
| `l3simple-official` #1 | 简化版 (0.3.6) | **partial 9.75%** (78/800) | `xnack(0x8)`（node31） |
| `l3simple-official` #2 | 简化版 (0.3.6) | **partial 9.88%** (79/800) | `xnack(0x8)` + `memory violation(0x4)`（node31/36） |

**崩溃点三次高度一致：~78/800 请求 = 80 客户端 round0 完成、首次 L3 批量写回（batch_set）时刻** ——近乎确定性。

对照组（同拓扑）：
- **L2（无 L3 写回）**：稳定至 **131K input tok/s**（256c@32 全绿）——L1+L2 写回不触发
- **L3（+RDMA→mooncake 写回）**：两种架构、两个 mooncake 版本、三种拓扑（含 09-27 的
  pp4 与单机 pp8）**全部同族崩溃**

## 3. 结论

1. **L3 崩溃与拓扑/架构/mooncake 版本均无关**：唯一变量是"是否存在 L3 写回路径"——
   GPU→Host→**RDMA batch_set** 段触发 MACA 平台非法访存（xnack 0x8 / memory violation 0x4）。
2. **PP 切割不是 L3 崩溃的原因**（pp1 最优拓扑下同样崩）；PP 影响的是历史上 pp4+L2 形态
   的稳定性（该问题已被最优拓扑解决）。
3. **当前可用的最优层级 = L1+L2**（最优解拓扑 + l2-pp1 镜像）：稳定（131K tok/s）+
   收益（64c TTFT -19.6%、multiturn round1 -51~-73%）。
4. **L3 生产化被 MACA 平台 bug 阻塞**——报障证据链现已完整：5 种签名 × 3 拓扑 × 2 架构 ×
   2 个 mooncake 版本 × 确定性复现点（首次 batch_set）。

## 4. 交付与复用

- 部署：`deepseek-hicache-l3/`（Dummy Client，pp1 改造）+ `deepseek-hicache-l3-simple/`
  （简化版，官方 Quickstart 对齐）；镜像 `r4-hicache-l2-mc013-pp1`（digest 8edf8b52）
- 场景：`hicache-multiturn-80x10-r16`（官方案例，L2/L2+L3 必含）、`hicache-l3-multiturn-*`
- 规范与排障：skill `sglbench-hicache-testing`（L3 全部坑：版本配对/端口/pp_size_default/
  dp-attention/启动卡死处置）
- 数据：DuckDB labels `l3on-*` / `l3simple-*`；env 文件 `envs/hicache-l3{,-simple}.json`
