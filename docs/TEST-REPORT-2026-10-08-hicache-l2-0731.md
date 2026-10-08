# TEST-REPORT — 0731 (0.5.14) HiCache L2 启用测试 + 与 0.5.13 对照

- **日期**：2026-10-08/09 深夜（Phase A 23:05–23:55；Phase B 23:55–00:18）
- **部署**：`deepseek-v4-flash-0731-pp1/lws/`（explicit 直启；4P+4D tp8/pp1/dp4 + DSPARK）
- **引擎**：decode `…-dsv4-0731-retractfix`（digest fb9cff47）；prefill 官方 0.5.14 + 本轮新增 HiCache L2
- **模型**：`/workspace/data/DeepSeek-V4-Flash-0731-W8A8`（LLMC SmoothQuant W8A8）
- **调优态**：max-running=40（10-08 标定）；context 400K；swa ratio 0.1
- **HiCache L2（prefill-only）**：`--enable-hierarchical-cache --hicache-ratio 0.5
  --hicache-mem-layout page_first --hicache-io-backend direct` + 伴生
  `SGLANG_ENABLE_UNIFIED_RADIX_TREE=1`（引擎实测改写 layout→`page_first_direct`）
- **labels**：L2-off `dsv4-0731-l2off-*`（envs/dsv4-0731-pp1.json）；L2-on `dsv4-0731-l2on-*`
  （envs/dsv4-0731-pp1-l2.json）；历史对照 `l2off-opt*`/`l2pp1-opt*`（0.5.13, 10-05）

## 1. 接线要点（0.5.14 移植差异）

1. **0731 explicit launcher 无 HiCache 支持**（0.5.13 为厂商脚本驱动）→ launcher 补注入块
   （`PD_ENABLE_HICACHE=1` 时追加 4 个 hicache flag）。
2. **`SGLANG_ENABLE_UNIFIED_RADIX_TREE=1` 必配**：0.5.14 的 `hiradix_cache.py` 仍不支持
   DSv4 混合 attention（"only supports MHA, MLA, DSA…"），server_args 校验报错直指该 env →
   走 UnifiedRadixCache（实测 `impl=UnifiedRadixCache hybrid_swa=True hierarchical=True`）。
   launcher 在 hicache 分支内 export（L2-off 路径零影响，保单变量纪律）。
3. io-backend 采用 0.5.13 后期最终标定 **direct**；引擎把 layout 改写为 `page_first_direct`。
4. 稳定性：L2 全链路 12 run 零崩溃零失败（retractfix 镜像 + L2 组合安全）。

## 2. mini-l2（随机 prompt，无前缀复用 → 预期无收益，测 L2 的干扰水平）

| 场景 | L2-off TTFT（×n） | L2-on TTFT（×n） | ΔTTFT | L2-off out | L2-on out |
|---|---|---|---|---|---|
| mini-32c | 5.00s（1） | 4.51s（1） | -9.8% | 1538.8 | 1665.5 |
| mini-64c | 11.56 / **13.61s** | 14.00 / 13.49 / 13.67s | **±无差异** | 2039.1 / 1958.6 | 2004.3 / 2061.5 |
| mini-128c | 2.44s（1） | 2.00s（1） | -18.0% | 1786.8 | 1804.5 |

- **单样本波动 ±15-18%**（64c 同配置 11.56 vs 13.61s）→ 初测的「64c L2 劣化 +21%」**经复测撤回**：
  on/off 均值 13.7 vs 12.6s，在噪声内。32c/128c 的单样本小改善同样不可断言。
- **结论（统计口径）**：随机 prompt 场景 L2 **无可靠性能效应**（预期内，无复用）；亦无劣化证据。
- 附带修正：与 0.5.13 的「格局反转」不成立——在噪声水平下两侧都只是 ±波动。

## 3. multiturn（前缀复用 → L2 真实试验场）

### 3.1 20×10（2K 工作集 < GPU L1 → L2 不触发）

逐轮 mean TTFT（s）：0.5.14 off `0.43 6.56 1.55 5.85 …` vs on `0.47 5.29 5.04 1.38 …`；
0.5.13 对照 off `0.61 5.18 1.72 …` / on `0.66 1.41 1.57 …`。**结论：稳态轮次两侧持平
（0.6-1.3s），早期轮次（r1-r3）出现 1.4-9s 尖峰的分布与配置无关 → 属跨运行的
JIT/热身状态伪影**（见 §3.2 的受控证据）。该场景工作集装得下 L1，本就不该有 L2 效应。

### 3.2 80×10（2K×80 > GPU L1 → 判别性场景）

逐轮 mean TTFT（r0–r9，s）：

| run（热身状态） | r0 | r1 | r2 | r3 | r4 | r5 | r6 | r7 | r8 | r9 |
|---|---|---|---|---|---|---|---|---|---|---|
| 0731 L2-off（roll 后冷） | 0.46 | **9.16** | 6.78 | 9.38 | 2.45 | 2.80 | 1.61 | 1.46 | 1.37 | 1.78 |
| 0731 L2-on run1（**组已验证充分热身**） | 0.21 | **2.77** | 2.55 | 2.33 | 1.99 | 1.70 | 1.79 | 1.28 | 1.55 | 1.40 |
| 0731 L2-on r2（roll 后冷） | 0.44 | **9.05** | 2.85 | 2.45 | 7.06 | 2.64 | 2.08 | 2.14 | 1.85 | 1.25 |
| 0.5.13 L2-off | 0.22 | 7.15 | 2.51 | 2.45 | 1.58 | 1.94 | 1.81 | 1.98 | 1.41 | 1.10 |
| 0.5.13 L2-on | 0.26 | 3.53 | 2.61 | 2.83 | 1.71 | 1.91 | 1.90 | 1.89 | 2.05 | 1.20 |

- **⚠ 关键混杂（本轮的测量学修正）**：r1 尖峰 ≈ **冷启动伪影**——同为 L2-on，冷态 r1=9.05s vs
  热态 2.77s（9.16s ≈ L2-off 冷态）。**稳态轮次（r5-r9）才是可用比较**：L2-off 1.37-2.80s vs
  L2-on 1.25-2.14s → **温和 L2 正收益**（前缀命中削减 prefill），幅度 ~10-30%。
- **对 0.5.13 历史结论的连带修正**：其「r1 -51% 断崖」与本轮的「-70%」同属热身混杂候选，
  在受控复测（双侧同等充分热身）前不应再引用为纯 L2 收益。**专项复测已列入后续遍历计划。**

## 4. 相比 0.5.13「是否提高」——分层口径

> 变量差异：引擎 0.5.14 vs 0.5.13、模型 LLMC vs FlexSMQ、DSPARK 有/无、max-running 40 vs ~160、
> context 400K vs 131K → 差异为 **system-level**，不能单独归因。且 out_tp 含 EOS 长度方差
> （同 wall 下均长可差 ±20%，见 §5 附注）。

| 场景 | 0.5.13 out tok/s | 0.5.14 out tok/s | TTFT 0.5.13 → 0.5.14 |
|---|---|---|---|
| mini-32c | 980.9 | 1538.8 | 7.66s → 5.00s |
| mini-64c | 1676.6 | 2039.1 | 3.38s → 11.56-13.61s ⚠（cap 差异，见 §5） |
| mini-128c | 1715.1 | 1786.8 | 1.38s → 2.44s |

- **吞吐**：0.5.14 三档全面更高（+4% ~ +57%），但含上列混杂；
- **TTFT**：64c 一档 0.5.14 明显更差——归因见 §5（cap=40 vs ~160 的队列效应，非引擎慢）。

## 5. 结论与建议

1. **L2 在 0.5.14 上已可用**（接线 + 验证 + 12 run 零故障）；收益画像：
   - 随机 prompt（无复用）：无效应（±噪声）；
   - 多轮复用场景：稳态轮次温和正收益（~10-30%）；
   - **建议：按业务场景切换**（`PD_ENABLE_HICACHE=1/0` 单 env 一键，prefill 单次滚动 ~6min）；
     纯短请求高并发（64c 级）无收益，可省。
2. **TTFT 的决定因素 = max-running 帽 vs 客户端并发**：
   - 不排队水平（cap ≥ 并发）：**2-4s**（mini-128c 2.0-2.4s、0.5.13 64c 3.4s、单流 1.24s 实证）；
   - cap 40 < 并发 64：队列使 TTFT 11.6-13.6s（0.5.13 cap~160 同场景仅 3.4s）；
   - **代价结构**：cap 为过载吞吐而设（conc-64-deepseek 1901 tok/s @40），与中负载 TTFT 是同一旋钮。
3. **下一步 = 参数遍历**（本报告后立即执行）：max-running {40/64/80/160} × {中等并发 TTFT / 过载吞吐}
   矩阵 + conservativeness 0.3 + L2 受控 A/B（双侧充分热身）→ 输出「最优 TTFT 档位 / 最大吞吐
   tok/s / 达成条件」最终结论。

### 附注：EOS 长度方差（贯穿性测量警告）
`conc-64-deepseek` rung40 vs rung80 复核：同 wall（~147s）总输出 282.8K vs 225.1K tokens
（均长 1414 vs 1125）→ 吞吐比较必须同 workload 长度或复测取中位，单对单 ±20% 不可判。

## 附录 A：复现命令

```bash
# L2 切换（prefill）：PD_ENABLE_HICACHE=1/0（launcher-configmap 已支持）
# 场景（L2-on）：
python3 -m sglbench run --scenario mini-l2-64c --exec kubectl --label dsv4-0731-l2on-mini64 \
  --env-file envs/dsv4-0731-pp1-l2.json --set model=DeepSeek-V4-Flash --set tokenizer=/workspace/data/DeepSeek-V4-Flash-0731-W8A8
python3 -m sglbench multiturn --scenario hicache-multiturn-80x10 --label dsv4-0731-l2on-mt80 \
  --scenario-file envs/scenarios-0731-override.json --env-file envs/dsv4-0731-pp1-l2.json
```
