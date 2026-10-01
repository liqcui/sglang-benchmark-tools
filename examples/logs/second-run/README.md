# second-run —— 多 run 对比补充数据

本目录是 `examples/logs/` 的补充，用于演示**同一场景多次 run 之间的对比**。
数据同样是 `tools/make_examples.py` 生成的合成数据，不是真实压测结果。

## 内容

| name | scenario_key | 说明 |
|---|---|---|
| `single-standard__second` | `single-standard` | `single-standard` 场景的第三次 run |

## 与批次的对应关系

| run | 文件位置 | Mean TTFT | Mean TPOT | 结论 |
|---|---|---|---|---|
| baseline | `../single-standard__baseline.log` | ≈ 84 ms | ≈ 7.7 ms | 基准 |
| candidate | `../single-standard__candidate.log` | ≈ 93 ms | ≈ 7.8 ms | TTFT +10%（回归），TPOT 在噪声内 |
| **second** | `single-standard__second.log` | ≈ 85 ms | ≈ 7.7 ms | 与 baseline 基本一致，无显著差异 |

把它作为第三个 run 一起做统计对比时，可以观察到：
`second` 与 `baseline` 的 TTFT 差异远小于 run 间正常波动，不应判定为变化；
而 `candidate` 的 TTFT 偏移明显超出 `baseline`/`second` 之间的散布，属于真实回归。

## 使用方式

与批次一完全相同：`.log` 给出汇总结果块，`.requests.jsonl` 给出每请求明细，
两者可用同一套脚本解析与对账。示例代码见 `../README.md`。
