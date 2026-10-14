# TEST-REPORT — 0731 PP4（官方脚本模式）vs PP1（最优解）全面对比

- **日期**：2026-10-10（战役 06:29–08:59；含 10-10 凌晨 v1 阶段）
- **目的**：回答「沐曦官方脚本推荐默认拓扑（PP4）与我们的 PP1 最优解，哪个性能更优」
- **口径声明（重要）**：本对比两侧为 **PP4 ⇒ 官方 llm-launch 脚本模式（official）** vs
  **PP1 ⇒ explicit 直启（当前生产配置）**——即对比**捆绑了「启动模式 + prefill 拓扑」两个变量**。
  未做交叉控制组（PP4-explicit / PP1-official 未跑，列为后续项）。
  另：PP4 侧的 ok 数据多来自**崩溃重试后的冷组**，理论上对该侧不利；即便如此 PP4 仍全面落后，
  **结论方向稳健**。
- **两侧配置**：

| 项 | PP4 侧 | PP1 侧 |
|---|---|---|
| 镜像 | `…-dsv4-0731-llm-retractfix`（digest 46db2551） | `…-dsv4-0731-retractfix`（fb9cff47） |
| 启动 | official（镜像内 llm-launch 脚本） | explicit（pd-launcher 直启） |
| prefill | **pp4 / tp8 / dp1**（厂商默认，tp=32/pp） | pp1 / tp8 / dp4 |
| decode | pp1/tp8/dp4/ep1 + DSPARK6（与 PP1 对齐） | pp1/tp8/dp4/ep1 + DSPARK6 |
| 并发 | decode 16×dp4=64 总；prefill pre_batch 64 | 双角色 max-running=64 |
| L2 / 其他 | L2 off；同模型（LLMC）/400K/16384/0.6 | 同左 |

## 1. 性能对比（ok 格；数字=mean TTFT / p99 TTFT / out tok/s）

| 场景 | PP4-official | PP1 最优解 | 胜者 |
|---|---|---|---|
| mini-32c | **2.88s / 7.4s / 1656** | 4.68s / 19.7s / 1595 | **PP4**（TTFT -38%） |
| mini-64c | 17.83s / 48.5s / 1462 | **4.36s / 9.1s / 1907** | PP1（**TTFT 4.1x**，+30% tok/s） |
| mini-128c | 无 ok（partial 7.2s/81失败） | **1.70s / 7.5s / 1800** | PP1 |
| conc-32 | 7.42s / 38.1s / 1438 | **2.43s / 7.4s / 1752** | PP1（3.1x，+22%） |
| conc-64（×2） | 11.15s / 23.4s / 1675 | **5.96 / 7.41s / 1877~2112** | PP1（~1.7x，+15~26%） |
| conc-128-safe | 31.09s / 52.9s / 1414 | **12.84s / 28.1s / 2008** | PP1（**2.4x，+42%**） |
| multiturn-20x10（r9） | 0.75s | 0.70s | 持平 |

**格局**：除 mini-32c（轻载短请求）以外，**PP1 在 TTFT（1.7~4.1x）与吞吐（+15~42%）全线领先**；
负载越重差距越大（conc-128: TTFT 2.4x / 吞吐 +42%）。

## 2. 稳定性对比（战役日志统计，直接来自 bench 结果）

| 侧 | 场景格数 | 总尝试 | 非 ok 尝试（失败/partial） | 从未取得干净数据的格 |
|---|---|---|---|---|
| **PP4-official** | 8 | **24** | 16（67%） | mini-128c、conc-64r2 |
| **PP1 最优解** | 8 | 9 | 1（b128-a1，77/300 崩溃） | — |

- PP4 侧约**每次尝试即伴随一次崩溃/重建**（平均 3 次尝试才拿到 1 格干净数据）；
  按战役时间计崩溃频率约为 PP1 的 **6-10 倍**。
- 崩溃族谱：prefill/decode 进程异常退出 → gloo 集合通信级联（"Connection closed by
  peer"）→ LWS 整组重建。首死者签名在可靠捕获中出现 `AttributeError: 'NoneType'
  object has no attribute 'server_args'`（该签名同样在 PP1-explicit 历史事件中出现过
  → **引擎广泛存在的未定位竞态**，非 PP4 专属；PP4 拓扑/官方模式显著放大其触发率）。
- ⚠ 工具缺陷（待修）：sglbench forensics 存在 **stale 捕获**（两次不同事件的 node
  log 快照 md5 完全相同）→ 事件级归因暂以战役统计与引擎日志为准。

## 3. 结论

1. **总胜者：PP1 最优解**——稳定性（尝试成功率 8/9 vs 8/24）与性能（TTFT/吞吐全线）双维度胜出。
2. **PP4-official 的适用边界**：仅 ≤32c 短请求轻载下 TTFT 略优（2.88s）；进入 64c 级
   及以上负载即频繁崩溃 + TTFT/吞吐显著劣化 → **不建议在本集群生产使用**。
3. 与历史结论一致：rc3 时代弃用 pp4 形态（吞吐 4.1x 劣势）的判断在 0731 + 官方脚本
   路径下**再次被独立验证**。

## 4. 工程沉淀（本轮新增）

1. **官方脚本模式在 0731 全链路打通**：`deepseek-v4-flash-0731-official-pp4/`（双模式
   launcher += r4 血统；镜像 `-llm-retractfix` 已推）。
2. **0731 llm-launch 适配补丁遗漏**：`get_pp_layer_partition`（pp>1 必需）→ launcher
   运行时注入（`11,11,11,10`）；**待固化进镜像**（apply_0731_llm_launch_patch.py）。
3. **leader 漂移自愈**：rank0 落点随组重建漂移 + router 同步 + scale 0→1——已内置进
   战役脚本；夜间曾因缺失该步骤导致 5h 假死（E2E 503），教训入 skill。
4. **官方 vs 显式并发语义**：decode `max_running_requests_specific` 为**每 DP rank**
   （×dp=总并发）；explicit 直传为总量。

## 5. 后续项

- [ ] `NoneType.server_args` 竞态定位与修复（两侧均触发，PP4 高频）——第 4 个引擎缺陷候选
- [ ] sglbench forensics stale 捕获修复
- [ ] 变量隔离：PP4-explicit / PP1-official 交叉控制格（如结论需剥离模式因素时）
- [ ] `get_pp_layer_partition` 固化进 -llm 镜像
- [ ] PP4 侧 mini-128c / conc-64r2 无干净数据（连续崩溃），如需要可低速率重跑

## 附录：复现

```bash
# PP4 侧
kubectl apply -k deepseek-v4-flash-0731-official-pp4/lws/   # 就地滚动切换
# leader 同步（每次重建后必做）
kubectl -n metax-ai-pd set env deploy/deepseek-v4-0731-router \
  PD_PREFILL_ENDPOINTS=$(kubectl -n metax-ai-pd get pod prefill-dsv4-0731-0 -o jsonpath='{.status.podIP}'):8000 \
  PD_DECODE_ENDPOINTS=$(kubectl -n metax-ai-pd get pod decode-dsv4-0731-0 -o jsonpath='{.status.podIP}'):8200
kubectl -n metax-ai-pd scale deploy/deepseek-v4-0731-router --replicas=0 && sleep 5 && kubectl -n metax-ai-pd scale deploy/deepseek-v4-0731-router --replicas=1
# 场景（label dsv4-0731-pp4off-*）
python3 -m sglbench run --scenario conc-64-deepseek --exec kubectl --label dsv4-0731-pp4off-b64r1 \
  --env-file envs/dsv4-0731-pp4off.json --set model=DeepSeek-V4-Flash --set tokenizer=/workspace/data/DeepSeek-V4-Flash-0731-W8A8
# PP1 侧
kubectl apply -k deepseek-v4-flash-0731-pp1/lws/  # + cap64 patch（见 lws/README）
```
