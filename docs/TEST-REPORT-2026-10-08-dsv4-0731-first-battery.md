# TEST-REPORT — DeepSeek-V4-Flash-0731 (0.5.14 官方镜像 + LLMC 新模型) 首轮全套压测

- **日期**：2026-10-08（battery 08:58Z 启动 → 13:38Z 全部收官，6/6 完成）
- **部署**：`deepseek-v4-flash-0731-pp1/lws/`（explicit 直启；4P+4D，tp8/pp1/dp4 + DSPARK）
- **镜像**：官方 `0.5.14-…amd64` 原版零补丁首测；**10-08 晚 decode 已切
  `0.5.14-maca.ai3.8.2.122-dsv4-0731-retractfix`**（retract 崩溃最小补丁，digest `fb9cff47`；
  prefill 保持官方镜像）——详见 §3
- **模型**：`/workspace/data/DeepSeek-V4-Flash-0731-W8A8`（沐曦适配版 LLMC SmoothQuant W8A8）
- **env 快照**：`envs/dsv4-0731-pp1.json`（labels `dsv4-0731-new-*`）
- **入口**：deepseek-v4-0731-router `10.66.3.32:8001`；served-name `DeepSeek-V4-Flash`
- **重要前提**：本轮为**"1:1 对齐用户单机验证命令"配置**（`max-running-requests=10`、
  `cuda-graph-max-bs=8`、`context 400K`、`conservativeness 0.6`）——即**按配置如实测量**，
  非集群调优后基线。

## 1. 结果总表（6/6 全部请求 100% 成功、零失败）

| scenario | out tok/s | mean TTFT | p99 TTFT | mean TPOT | 时长 |
|---|---|---|---|---|---|
| single-deepseek-standard | 70.2 | 1.24s | 6.90s | **13.6ms** | 24min |
| single-longctx-deepseek (65K) | 70.0 | 13.3s | 15.9s | **12.7ms** | 39min |
| conc-32-deepseek | 675.0 | **62.6s** | 111.4s | **10.7ms** | 10min |
| conc-64-deepseek | 660.5 | **130.6s** | 234.0s | **10.5ms** | 10min |
| conc-128-deepseek-safe | 145.4 | 147.1s | 338.9s | **286.1ms** ⚠ | 70min |
| conc-32-longctx-deepseek (32c×65K/8K) | 112.8 | 20.1s \* | 305.5s | 224.6ms ⚠ | 121min \* |

> \* 第 6 场：bench 底层实际 **100/100 零错误完成**，但完成时刻超出 sglbench wrapper（`--timeout 2h`）
> 仅 **62s**（实测 duration 7261.6s）→ run.json 记 failed(timeout)、stdout 结果块随管道丢失。
> **完整指标已从 pod 结果对象恢复存档**：`data/runs/conc-32-longctx-deepseek-20261008T113706Z-3d69/recovered-metrics.json`。
> mean TTFT 20.1s 受 ttft≈0 测量伪影（PD 模式首 chunk 提前返回）拉低——**尾部真实排队 TTFT
> 达 200–313s**（p99 305.5s），以该值为准。下次该场景应 `--timeout >2.2h`。

## 2. 关键分析

### 2.1 DSPARK 引擎收益（真实提升）✅
- **单流 TPOT 10.5–13.6ms**（旧 rc3-0.5.12 为 16.5–28.9ms，**约 2x 改善**）
- 单流吞吐 70.2 vs 59.3 tok/s（+18%）；长上下文单流同为 70 tok/s / 12.7ms
- 与用户单机验证一致（86-90 tok/s 单流差距来自 PD 链路开销 + 本轮 context 400K/页面配置）

### 2.2 并发瓶颈 = `--max-running-requests 10`（1:1 单机验证参数）⚠
- 32/64 并发下聚合锁死在 **~660–675 tok/s**：
  由 10 并发 × ~10.6ms/步 的引擎上限 ≈ 700–950 tok/s 决定（实测 660-675 吻合）
- TTFT 全部为**排队时间**：62.6s @32c ≈ (32-10)/10 × 单请求服务时长，
  130.6s @64c 进一步翻倍 —— 线性排队，非引擎故障
- 单并发 p99 TTFT 6.9s（1.24s 均值）—— 存在长尾抖动（DSPARK graph 捕获/首请求冷启动），待查

### 2.3 conc-128 病态退化 ⚠（需调优后复测）
- out 仅 145 tok/s、TPOT 冲到 **286ms**（引擎内部抖动/回收churn）、单场 70 分钟
- 疑似：10-cap + 128 排队 × `schedule-conservativeness=0.6` → retract/abort churn、
  预填充浪费；该配置不适合高并发，属**参数问题**而非引擎缺陷（待单变量实验验证；
  §2.5 的 ITL-TPOT 分裂为该假设提供直接证据）

### 2.5 长上下文（32c×65K）画像：每步效率不变，wall 时间被排队/调度间隙吃掉 ⚠

- **100/100 零错误**；但总时长 **121min**（超 wrapper 2h 上限 62s，见 §1 脚注）
- 聚合 **out 112.8 tok/s / in 902.5 tok/s**（峰值 out 457 tok/s）——对比短上下文
  660–675 tok/s，长上下文并发吞吐塌缩 ~6x
- **引擎单步健康**：mean ITL **11.7ms** / p99 ITL 31.3ms（与单流 longctx 12.7ms 同级）
  → DSPARK 解码在 65K × 32 并发下不降速
- **但 wall-TPOT 224.6ms（p99 378.6ms）≈ 20× ITL** —— 差值即请求在途的排队/调度间隙；
  疑与 max-running=10 下长 KV 请求间反复让位（retract/换出）相关，与 conc-128 病态
  （TPOT 286ms）同源机理。e2e：mean 31min / p90 50min / p99 51.7min
- **结论**：长上下文是调优阶梯后最值得复测的档位；当前配置下 32×65K 并发不可用

### 2.4 与旧生产最优解对照（config-vs-config，注意变量）

| scenario | 旧 rc3-0.5.12（max-running 160） | 新 0731-0.5.14（max-running 10） |
|---|---|---|
| single | 59.3 tok/s / TTFT 841ms / TPOT 16.5ms | 70.2 / 1.24s / **13.6ms** |
| conc-32 | 1078 / TTFT 2.2s / TPOT 26.0ms | 675 / **62.6s** / **10.7ms** |
| conc-64 | 1747 / TTFT 4.3s / TPOT 28.9ms | 660 / **130.6s** / **10.5ms** |
| conc-128 | 2050 / TTFT 12.2s / TPOT 26.7ms | 145 / 147s / **286ms** |

→ 新引擎**每 token 效率全面更优**，但并发能力被配置锁死；**不可比吞吐，只能比单流**。

## 3. 调优执行结果（E1+E2 阶梯，10 → 40 → 80）

### 3.1 🚨 插曲：rung-40 首测两次全组崩溃 → 根因定位 → 最小补丁修复

`max-running=40` 首次开测，conc-64 负载 60-80s 内 decode 整组崩溃 ×2（bench 零样本）。
四次 rank traceback 完整，机理链（与 0.5.12 2026-10-02 双缺陷完全同款）：

```
KV 压力 retract → schedule_batch.py:1619 无条件 offload_kv_cache
 → swa.py:382 get_cpu_copy → memory_pool.py:1221 NotImplementedError（混合 SWA 池未实现）
 → scheduler 崩 → gloo MLP-sync all_gather 级联全 rank（Connection closed by peer）
 → http-server 重入 DuplicateTimeseries → 整 pod 退出 → LWS RecreateGroup 整组删建
```

**修复**（最小改动，2 文件 3 处，对齐 0.5.12 patch 0001+0002）：
① `retract_decode` 无 offload 能力时 retract→`FINISH_ABORT` 优雅降级（客户端 500，服务不死）；
② `release_req` offload 调用加 `disaggregation_decode_enable_offload_kvcache` gate；
③ `func_timer.py` Histogram dedup guard（防 DuplicateTimeseries 放大）。
镜像 `0.5.14-maca.ai3.8.2.122-dsv4-0731-retractfix`（digest `fb9cff47`）；验证链：pristine
干跑 → 镜像内断言 → pod 内 marker。**复测：同一负载 100% 成功、零崩溃。**

### 3.2 调优曲线（conc-64-deepseek，200 req，修复后引擎）

| max-running | out tok/s | 成功 | mean TTFT | p99 TTFT | mean TPOT | mean ITL | 结论 |
|---|---|---|---|---|---|---|---|
| 10（battery 基线, 官方镜像） | 660.5 | 100% | 130.6s | 234.0s | 10.5ms | — | 排队锁死 |
| **40（🏆 推荐, 修复镜像）** | **1901.5** | **200/200** | **12.7s** | 27.9s | 25.6ms | 17.3ms | **2.88x，TTFT -90%** |
| 80（修复镜像） | 1542.7 | 200/200 | 13.6s | 35.1s | 21.3ms | 20.0ms | 回退 -18.9% → 阶梯止 |

- **rung-40 = 最优**（吞吐 2.88x、TTFT 130.6→12.7s、零失败零崩溃）；rung-80 吞吐回退 18.9%，
  按 <5% 收益规则终止（160 未跑）；两档 **均零 abort**（KV 池未见压力；优雅降级路径未触发）
- ⚠ 单样本 + EOS 长度方差（各 boot random_seed 不同 → 实际输出长度 ~930 vs ~1050 tokens）
  → rung-80 回退幅度含此噪声；主指标（吞吐/TTFT）两者一致指向 40 更优
- ⚠ 副作用记录：`--cuda-graph-max-bs 40/80` 均被引擎自行压回 **10**（"adjusted to 10"），
  E2 的有效上限即 10；rung-40 的 p99 TPOT 201ms 长尾（尾请求调度间隙）观察项

## 4. 下一步

1. ✅ battery 6 行全齐（§1）；✅ E1 阶梯执行 + retract 崩溃修复闭环（§3）
2. ✅ 推荐参数（max-running=40 / graph-bs=40）已回写 live LWS + `lws/base/*.yaml`
3. 可选：调优后复测 conc-128 / conc-32-longctx（@40）；与旧 rc3 基线出正式对照报告
4. E3–E5（conservativeness / chunked-prefill / context）未动，按需再单变量
