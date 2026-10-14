# 场景评审：4.1「标准压测场景（6 × 2 模型）」

- **评审对象**：README §4 / `sglbench/scenarios.py::_catalog()` 中的 6 个标准场景（DeepSeek 侧实跑）
- **评审基准**：本仓库自身实测档案 —— `TEST-REPORT-2026-10-08-dsv4-0731-first-battery.md`（首轮 battery）、
  `TEST-REPORT-2026-10-12-stable-envelope-0731.md`（稳定包络）、
  `TEST-REPORT-2026-10-10-0731-comprehensive.md` §9（pernode 终版最优配置）
- **原始需求**：用合理的并发 / 速率 / 标准，定位生产环境"**首先不崩溃 → 最优 TTFT/TPOT/token/s**"的操作点，
  覆盖低/中/高并发，速率不能过低，并测出安全运行边界
- **结论**：6 行**作为"链路健康回归基线"基本可用**（15 行中 3 行需改参数、2 行需改语义、1 行需降级为探针），
  但**作为"定位最优解/边界"的实验设计不成立** —— 缺一个关键维度（固定并发扫速率）、
  并发档位与形状容量点脱钩、速率列在多数行里不是控制变量。详见 §3。

> 引用说明：下文所有"实测"数字均来自上述报告，未做二次测量；单位 tok/s 一律指**输出**吞吐，
> 除非明确写"输入"或 total。

---

## 1. 速览判定

| # | 场景 key | 判定 | 一句话理由 |
|---|---|---|---|
| ① | `single-deepseek-standard` | 🟡 **改参数** | 语义正确（单并发能力基线）；但 n=50 的 p99≈最大值、24min 只换来 1 个统计点；输出长度受 EOS 早停影响（§3.6） |
| ② | `single-longctx-deepseek` | 🟡 **改名 + 改参数** | 表头写"200K 基线"，实际输入 **65536（65K）**；真 128K/200K 档在 `prod-longctx-*`。单并发语义本身合理 |
| ③ | `conc-32-deepseek` | 🟢 **保留语义，去掉"Decode 满配"因果** | 闭合环 32 并发有效；但"速率 4.0"在 `rate×S ≈ 92 > 32` 时只是"填满"用，不是负荷；TTFT 62.6s 的旧值全部来自 `max-running=10` 而非本档 |
| ④ | `conc-64-deepseek` | 🟢 **保留语义，同上** | 同 ③；实测 TTFT 130.6s / 吞吐 660.5 是 cap=10 的排队产物，cap→40 后同档 12.7s / 1901.5 tok/s（2.88×） |
| ⑤ | `conc-128-deepseek-safe` | 🔴 **改语义：不是"安全档"** | 旧代实测属**黄区**（rate 4–6 × conc 64–160，累计 3–4 次自愈崩溃；TTFT 147.1s / p99 338.9s / TPOT 286ms）；新代 pernode 下又变成舒适绿区（1.48s / 18.2ms）。"安全"二字两边都不成立 |
| ⑥ | `conc-32-longctx-deepseek` | 🔴 **降级为边界探针** | 65K 形状饱和并发 ≈ 8–11，用 32 并发是 **3× 过载**；实测 121min、p99 TTFT 305.5s、**超出 2h wrapper 仅 62s → run 记 failed**，指标靠 `recovered-metrics.json` 救回；且 `rate=2.0` = 该形状容量点的 16×，纯装饰 |

---

## 2. 评审基准：本集群实测容量与包络

任何"并发/速率是否合理"的判断都必须以容量为坐标系。本集群已标定（显式-PP1 @cap64）：

| 常量 | 实测值 | 来源 |
|---|---|---|
| 输入侧容量墙 `C_in` | ≈ **8.2K in-tok/s**（标称长度口径，见 §3.6） | 10-12 §4 |
| 输出侧容量墙 `C_out` | ≈ **2.0K**（PP1）/ **2.9–3.35K**（pernode c48）out-tok/s | 10-12 §3、10-10 §9.3 |
| 绿区（零崩溃） | `rate ≤ 2 且 conc ≤ 64` → TTFT ≤ 3.0s / TPOT ≤ 22.6ms / ≤ 2028 tok/s（累计 >15 run 0 崩溃） | 10-12 §3 |
| 黄区（可用·偶发自愈） | `rate 4–6 × conc 64–160`；conc128: 11.3s / 17.3ms / 2179 tok/s；累计 3–4 次崩溃，均自愈 ~8min | 10-12 §3 |
| 红区（禁用） | `cap=160`（引擎 boot 缺陷，曾致 9h 重建循环）；`conc > 160` 未测不建议 | 10-12 §3 |
| 速率历史安全线 | 4K 档 ≈ **5 req/s**；`rate ≥ 8` 从未通过 | 10-12 §3 |

**当前 live 最优配置（pernode + 每引擎 cap48 + DSPARK on，10-12 晚）**：

| 目标 | 配置 | TTFT | TPOT | out tok/s |
|---|---|---|---|---|
| 最优稳态 | rate 2 | **1.05s** | 14.8ms | 2903 |
| 吞吐峰值 | conc 160 @ rate 5 | 2.00s（p99 8.6s） | 18.9ms | **3354** |
| 单流能力 | conc 1 | ~0.8s | **5.5ms** | — |

→ 这张表就是需求里"最优解"的**当前答案**；6 行标准场景的问题不在于答案未知，
而在于**它无法复现/更新这个答案**（§3.1–§3.2），且换一代部署后数字全变（§3.4）。

---

## 3. 结构性问题（按影响排序）

### 3.1 ⭐ 缺"固定并发扫速率"这一维度 —— 而它才是定位容量点/安全边界的唯一手段

先看每行的实际控制变量。闭合环下，实际在途并发 `N ≈ min(max_concurrency, rate × S)`（S = 单请求服务时长）：

| # | conc | rate | S 估算 | `rate×S` | 实际控制量 | 速率列的作用 |
|---|---|---|---|---|---|---|
| ① | 1 | 1.0 | ~29s（旧）/ ~16s（新） | 29 / 16 | `conc=1` | 无（仅保证不空转） |
| ③ | 32 | 4.0 | ~23s | **92 > 32** | `conc=32` | 无 |
| ④ | 64 | 6.0 | ~46s | **278 > 64** | `conc=64` | 无 |
| ⑤ | 128 | 5.0 | ~39s | **194 > 128** | `conc=128` | 无 |
| ⑥ | 32 | 2.0 | 数十分钟 | ≫ 32 | `conc=32` | 无 |

**③④⑤⑥ 的"速率"列都不是控制变量**（客户端信号量先兜住，`rate` 只负责"把槽位填满"）。
后果有三：

1. 速率列里的 `4.0 / 6.0 / 5.0 / 2.0` 既无容量换算依据，也无单调规律（conc 翻倍到 128 时速率反而降到 5.0），
   读者会把"速率差异"误读为"负荷差异"——实际负荷差异 100% 来自 conc。
2. **整个表没有任何一行在扫速率**，因此无法回答需求里最核心的两个问题：
   "在哪个速率下 TTFT 开始恶化"（容量点）、"速率到多少会崩"（安全边界）。
3. 反过来，因为 `conc ≤ 引擎 cap`（新代 cap 192）时客户端排队对 TTFT 不可见（§3.3），
   表格会给出"rate 6.0 时 TTFT 依然很好"的**假象**——超出的负荷全部沉在客户端队列里，只体现为 run 时长。

**结论**：必须补两类实验，且一次只动一个变量：
- **开放环（rate 主导）**：`max_concurrency` 设到远大于 `rate × S`（不成为约束），扫 rate = `0.35/0.7/1.05/1.4/1.75 × r*`
  → 读出容量点（p99 TTFT 越过 SLO 的那个 rate）与吞吐平台。
- **闭合环（并发主导）**：`rate` 设到能填满槽位（`rate ≥ 1.5 × conc / S`），扫 conc
  → 读出吞吐最优并发、以及"再多也没收益"的拐点。现表 ③④⑤ 属于此类，是有效的，
  只是应把 `rate` 明确标注为"填满速率"并去掉它身上的负荷含义。

### 3.2 ⭐ 并发档位应与"形状饱和并发"挂钩，而不是所有形状统一 32/64/128

同一个并发数在不同形状下含义天差地别。饱和并发 `N_sat ≈ r* × S`，其中 `r* = min(C_in/L_in, C_out/L_out)`：

| 形状 | `r*`（req/s） | S 估算 | **N_sat** | 现表用的档位 | 偏差 |
|---|---|---|---|---|---|
| 4K/2K 通用 | ~1.4–2.0 | ~32s | ~45 | 32 / 64 / 128 | 合理 |
| 65K/8K 长上下文 | ~0.125 | ~87s | **~11** | **32** | **3× 过载** |
| RAG 8K/256 | ~1.0 | ~7s | ~7 | 无 | — |
| chat 512/512 | ~5.5 | ~9s | ~50 | 无 | — |

→ ⑥ 的灾难（121min / p99 TTFT 305.5s / wrapper 超时）不是"长上下文不安全"，而是**并发档位取错 3 倍**。
对长上下文形状，合理档位是 4/8/16，把 32 留给边界探针。
同理，RAG 形状上 32/64/128 三档会量到**同一个东西**（都远超 N_sat=7），纯属浪费机时。

> 口径说明：`N_sat` 依赖 `S`（单请求服务时长）估计，只有**量级**意义；其中 65K 档的 ~11
> 有独立佐证 —— 报告给出 full KV 池 202K tokens/rank、每请求吃 ~65K → **~3 请求/rank × 4 rank ≈ 12**。
> 其余档位请按实测 TPOT/TTFT 回填；`tools/scenario_budget.py` 会把这些换算与判定一次打印出来
> （`--ttft-ms/--tpot-ms` 可覆盖估计值）。

### 3.3 测量有效性：TTFT 里"能看见的排队"和"看不见的排队"

同一份 `conc-64` 数据在两种部署下差 **10×**：

| 场景 | 旧（`max-running=10`） | 新（cap 40） |
|---|---|---|
| conc-64 | 660.5 tok/s，TTFT **130.6s** | **1901.5 tok/s**，TTFT **12.7s** |

机制（报告 §2.2 已自行归因）：TTFT 计时从**请求真正发出**开始，
所以 **客户端信号量排队不计入 TTFT，服务端准入排队计入**。
当 `max_concurrency > 引擎 cap` 时，一部分请求排在客户端（不可见）、一部分排在服务端（可见），
两侧都不干净：既低估真实用户延迟，又让 TTFT 含义随 cap 漂移。

**对策**（二选一，写进场景前置条件）：
- 闭合环：`max_concurrency ≤ 引擎 cap`（保证无服务端排队，TTFT = 纯服务时延）；
- 开放环：`max_concurrency ≥ 2 × rate × S`（保证无客户端排队，TTFT 反映服务端饱和）。
并**把 cap / 拓扑 / env 文件写进 run 标签或场景 description**（见 §3.4）。

### 3.4 跨代不可比：场景缺少"前置条件"元数据

同一个 key `conc-160 @ rate 5`：旧跨节点 PD **13.35s**，pernode **2.00s**（6.7×）。
`max-running` 从 10 → 40 → 48 每动一次，历史数字就作废一次。
但 6 个标准 key 是"稳定标识"，其参数与前置条件却不含 **cap / 拓扑 / 镜像 / DSPARK 开关 / chunked 尺寸**。
→ 建议：场景 description 里带前置条件摘要（或统一用 `--env-file` + `--label` 记录）；
       跨代比较时以 `compare` 的"配置指纹"为准，而不是同名 key。

### 3.5 SLA 与实测脱节，门禁失真

`scenarios.py` 里的 SLA 是早期拍值，和实测差 1–2 个数量级（旧代数据）：

| 场景 | SLA p99 TTFT | 实测 p99 TTFT | 结果 |
|---|---|---|---|
| `single-deepseek-standard` | 5000ms | **6900ms** | ❌ 首轮就不过 |
| `conc-32-deepseek` | 6000ms | **111.4s** | ❌ |
| `conc-64-deepseek` | 10000ms | **234.0s** | ❌ |
| `conc-128-deepseek-safe` | 15000ms | **338.9s** | ❌ |

→ SLA 必须**由绿区包络反推**、且**按形状分别标定**（方法见 §6）。否则 `gate` 要么永远失败被忽略，
要么被改成"永远通过"，两种都失去卡点意义。

### 3.6 ⭐ 输出长度不受控（EOS 早停）—— 跨 run 的 tok/s 直接不可比

> **更正说明（2026-10-12 实测核查）**：本节初版曾判断"`random_range_ratio=1.0` 使长度在 `[1,L]`
> 均匀分布、实际均值 ≈ 标称一半"。**该判断错误，已撤回**：实测 267 个落库 run 的 `input_len`
> **恒等于标称值**（400+ 样本无任何分布），说明该参数在本集群版本下不产生长度抖动。
> 真正的原因见下 —— 输出侧的 **EOS 早停**。

**实测事实**（`tools/audit_run_lengths.py`，审计 `data/runs` 全量）：

| run（场景同为 conc-64-deepseek，标称 4096/2048） | 镜像时代 | 实际 out/标称 | 输出长度分布（每 256 token 分桶） | out tok/s |
|---|---|---|---|---|
| `…20261008T101451Z` | 旧 | **1.000** | 全部落在 2048 | 660.5 |
| `…20261008T142732Z` | 新 | 0.690 | 53 个 ≤256 + **134 个 =2048** | 1901.5 |
| `…20261008T165732Z` | 新 | 0.527 | 69 个 ≤256 + **91 个 =2048** | 1879.6 |
| `…20261012T105000Z`（pernode 稳态档） | 新 | 0.844 | 23 个 ≤256 + **166 个 =2048** | 2902.7 |

分布是**双峰**（≈0 处一堆 + 恰好在 cap 处一堆），不是均匀分布 —— 即
**`--random-output-len` 只是上限，生成被 EOS 提前终止**；随机 token 组成的提示词没有语义，
模型有时在头几十个 token 就吐 EOS（新镜像下 11–35% 的请求如此），其余跑满 2048。

**为什么这很严重**：

1. **跨镜像不可比**：同一个 key、同一组参数，10-08 下午镜像切换后的实际输出长度只有切换前的
   0.38–1.00 倍，**载荷与延迟数字全部随之漂移**，而场景定义里看不出任何差别。
2. **rate 受限时 tok/s 与长度成正比**：`out tok/s ≈ rate × 实际输出长度`。审计里
   `conc-64-deepseek` 76 个 run 有 **51 个（67%）长度未受控**，`out/nom` 在 **0.11–1.00** 之间；
   直接比较"1901 tok/s vs 660 tok/s"这类数字，会把长度差异当成配置收益
   （审计工具另给出"等效满长"列：1901.5 → 2754.1）。
3. TPOT/尾延迟统计同样被污染：早停请求与满长请求混在一起，p99 的分母不稳定。

**修法**（三选一，视目的而定）：

| 目的 | 做法 |
|---|---|
| 合成形状的容量/延迟实验（本套 v2 默认） | 加 `--ignore-eos`，强制生成到 `random_output_len`，长度确定、跨 run 可比 |
| 生产真实性验证 | 用真实数据集（`--dataset-name sharegpt`），长度来自真实分布 —— v2 已加 `v2-mixed-sharegpt-c64-r8/r16` 两档与合成定长档交叉验证 |
| 沿用历史数据 | 用 `tools/audit_run_lengths.py` 按"等效满长"列归一后再比，**并报告 mean_out** |

#### 3.6.1 一个更值得追的线索：截断与并发相关

按场景汇总（同一 key、跨 run）后，漂移**不是随机的**：

| 场景 | run 数 | 长度漂移 run | `out/nom` 区间 |
|---|---|---|---|
| `single-deepseek-standard` / `single-longctx-deepseek` | 17 | **0** | 1.00–1.00 |
| `conc-32-deepseek` / `mini-l2-32c` | 29 | **0** | 1.00–1.00 |
| `conc-64-deepseek` / `mini-l2-64c` | 90 | **65** | 0.14–1.00 |
| `conc-128-deepseek-safe` / `mini-l2-128c` | 37 | **21** | 0.11–1.00 |
| `conc-256-deepseek` | 4 | **4** | 0.49–0.52 |

**并发 ≤32 一次都没漂，≥64 大面积漂**，而且这些请求的 `status` 都是 ok（HTTP 200，非客户端错误）。
两种解释：(a) 随机 token 提示词里含类 EOS 词元 → 模型立即停；(b) **batch ≥64 时引擎提前结束部分请求**
（早停/被抢占后被当作正常完成返回）。若是 (b)，那不只是**度量问题**，而是**服务正确性问题**
（高并发下用户会拿到 1–60 token 的半截回答）。

**建议**：拿一轮 `--ignore-eos` 的 conc-64 对照 —— 若加 flag 后依然出现"1 token 完成"的请求，
就锁定为引擎侧问题，需按 batch 分层复现（b32/64/128 各固定种子跑一次）。这条已记入 §8。

> ⚠ `--ignore-eos` 的 flag 名需现场确认（本机无 sglang 源码、外网不可达）：首次跑用
> `--set num_prompts=8` 冒烟；若报 `unrecognized arguments: --ignore-eos`，删掉两套 JSON 里各行的
> `"extra_args"` 字段即可（工具与文档其余结论不受影响）。

### 3.7 统计功效与热身

| 问题 | 证据 | 建议 |
|---|---|---|
| p99 样本不足 | ② n=20、① n=50 → p99 实际是"最大值" | 延迟分位用 n ≥ 100；并发档 n ≥ 300（现 ③④⑤ 已是 200–300） |
| 长度方差主导噪声 | `conc-64-deepseek` 76 run 中 51 个长度未受控，`out/nom` 0.11–1.00（§3.6） | `--ignore-eos` 或真实数据集；结论必须附 mean_out |
| 单次运行 + 长度方差 | 报告 §5.3 铁律：`TTFT@64c 波动 ±15%` | 每档 ≥ 3 次取中位（v2 战役已用 `repeats`，见 §3.10）；报告里带离散度 |
| roll 后首跑热身伪影 | pernode 单流 3.71s vs 晚测 **0.82s**（JIT/graph 捕获） | 首跑丢弃或加 warmup 请求；跨 run 比较只用暖态 |
| 混杂变量 | E1 阶梯中 rung-40/80 各自 boot 的 `random_seed` 不同 → 输出长度 ~930 vs ~1050 | 固定长度后再比较（该现象即 §3.6 的 EOS 早停） |

### 3.8 时长与超时

⑥（100 req × 65K/8K × 32c）实测 **7261.6s = 121min**，wrapper `--timeout 2h` → **仅差 62s 超时**，
run 记 `failed(timeout)`、stdout 结果块随管道丢失（靠 pod 内结果对象恢复）。
→ 边界类/长上下文场景必须显式给足 `--timeout`（建议 3h），或降低 n/并发；
    并把"wrapper 超时"与"引擎崩溃"在分类上区分开（前者不是稳定性问题）。

### 3.9 双模型不对称

GLM 未部署，其 6 个 key 的 rate/SLA 是从 DeepSeek **原样抄来的保守值**（`scenarios.py` 注释自认）。
→ GLM 部署后必须按 §5 的流程重新标定容量点（模型不同 `C_in/C_out` 不同），
   否则会出现"GLM 场景达标/不达标"的假信号。保留对称 key 结构没问题，但别让抄来的数字进 `gate`。

### 3.10 压测框架自身的两个坑（已修复并验证）

评审过程中在 `campaign` 执行器里发现两处**静默失效**，都会让"按 suites 跑一遍"的结果失真：

| # | 问题 | 影响 | 修复 |
|---|---|---|---|
| 1 | `campaign run` 接受 `--scenario-file` 但**不传给场景解析**（`get_scenario(key)` 只用内置目录） | 自定义场景（v2-*、`prod-*` 之外的覆盖档）在战役里直接 `KeyError`；dry-run 也不校验 key，跑到一半才发现 | `run_campaign(..., catalog=...)` 贯通到 bench/multiturn runner；dry-run 显式校验 key 并给出可读报错 |
| 2 | `ScenarioEntry.repeats` **被解析但从未执行** | 既有 `campaigns/production-suite.json` 里 `{"repeats": 2}` 的边界探针实际只跑 1 次；"每档 ≥3 次取中位"的统计意图落空 | 执行循环按 `repeats` 重复执行并打印 `[i/N]` |

两处均已在本地验证：`repeats` 3+1+2 = 6 次调用序列正确；自定义 key 可解析；
未知 key 在 dry-run 报错；不传 catalog 时旧行为（含既有战役 spec）不变。

### 3.11 其他

- `sglbench run --scenario X --set ...` 的 `--set` 只接受**已存在的 Scenario 字段**，
  无法追加 `extra_args`（如 `--ignore-eos`）—— 需要由场景文件提供（v2 已内置）。
- 度量口径已核实：`output_token_throughput_tok_s` = **实测生成 token ÷ bench 自身 duration**
  （非标称 token、非 wrapper wall_s），所以数字本身是诚实的，问题只在**跨 run 可比性**（§3.6）。

---

## 4. 优化后的场景设计（三层）

一台压测机时有限，因此按"每次改动都跑 / 定位最优解时跑 / 定期钉墙"分三层。

### 层 A：链路健康回归（快，闭环节，替代现 ③④⑤）
固定形状 4K/2K，`rate` = 填满速率，扫并发阶梯 **16 / 32 / 64 / 96 / 128 / 160**
（补上 96 —— 历史 retract 线 ≈96、以及当前吞吐峰值 160，正是 64→128 的空白区）。
每档 n=300、成功 100%、p99 TTFT ≤ 4s（160 档放宽到 9s）。

### 层 B：容量点定位（开放环，新增 —— 需求里"最优解/安全边界"的正解）
固定形状 4K/2K、`max_concurrency=128`（大于 `N_sat≈45`，不成为约束），
扫 `rate = 0.5 / 1.0 / 1.5 / 2.0 / 2.5`（= 0.35 / 0.7 / 1.05 / 1.4 / 1.75 × `r*`），
**5 行共用同一个 SLO**（p99 TTFT ≤ 4s、p99 TPOT ≤ 30ms、0 失败）→ "通过的最高 rate" 即容量点，
"p99 TTFT 拐点" 即安全边界。每档 n=300（吞吐受限，跑满仅 ~4–10min）。
另加 ShareGPT 真实长度两档（c64 @ r8/r16）与合成定长档交叉验证 —— 它不受 §3.6 的 EOS 口径影响。

### 层 C：边界探针（known-failure，定期一次，勿入门禁）
- `conc 256 @ rate 8`（旧代红区；新代未测 >160）
- `65K × 32 @ rate 1.0`（原 ⑥ 降级而来，前置 `--timeout 3h`）

### 形状维度：从"只有 4K/2K + 65K"扩展到生产画像
| 画像 | 形状 | `r*`（本集群） | `N_sat` | 采集档位 |
|---|---|---|---|---|
| chat 在线对话 | 512/512 | ~5.5 | ~50 | c1（能力）/ c64 @ r5.5 / c128 @ r8.0 |
| standard 通用 | 4096/2048 | ~1.4–2.0 | ~45 | 层 A + 层 B |
| rag 知识问答 | 8192/256 | ~1.0 | ~7 | c1 / c16 @ r1.0 / c32 @ r1.5 |
| code 代码生成 | 1024/2048 | ~1.4 | ~45 | c1 / c64 @ r1.5 / c128 @ r2.2 |
| longctx 长上下文 | 65536/8192 | ~0.125 | ~11 | c1 / c8（稳）/ c16（边界）/ c32（探针） |
| mixed 混合真实长度 | ShareGPT 实测 in≈340/out≈215 | ~13.5 | ~31 | c64 @ r8.0 / r16.0（与合成定长档交叉验证，不受 §3.6 影响） |

（真 128K/200K 档沿用已有的 `prod-longctx-128k-c1` / `prod-longctx-200k-c1`，不必新增。）

### 具体产物
| 文件 | 内容 | 用法 |
|---|---|---|
| `scenarios/standard-v2.json` | 14 行：单流能力基线（standard/chat/code/rag/65K）+ 层 A 并发阶梯（16/32/64/96/128/160）+ 长上下文 c8（稳定）+ 2 个 known-failure 探针 | `sglbench run --scenario-file scenarios/standard-v2.json --scenario v2-conc-64-standard ...` |
| `scenarios/capacity-v2.json` | 15 行：层 B 速率扫描（5，同一 SLO）+ 形状容量档（chat/rag/code 各 2）+ ShareGPT 混合真实长度（2）+ 长上下文 c16 边界档 + 1 个联合边界探针 | `sglbench sweep --family v2-capacity` 出速率-时延曲线 |
| `campaigns/v2-standard.json` | 层 A 一条命令跑完（12 条目，关键档 `repeats` 2–3 次取中位） | `sglbench campaign run campaigns/v2-standard.json --side v2 --scenario-file scenarios/standard-v2.json` |
| `campaigns/v2-capacity.json` | 层 B 一条命令跑完（14 条目，速率点各 repeats 2–3） | 同上（`--scenario-file` 两个文件都带上） |
| `campaigns/v2-probe.json` | 层 C 三个 known-failure 探针（`timeout 3h`、`auto_retry 3`、`gpu_reset`） | 同上 |
| `tools/scenario_budget.py` | 负荷预算/实验有效性审计：per-scenario 的 tok/s 负荷、占墙比例、`rate×S` 与并发的关系、长上下文 KV 池上限，直接判出"开放环/闭合环/耦合" | `python3 tools/scenario_budget.py --scenario-file …` |
| `tools/audit_run_lengths.py` | 落库 run 的长度口径审计（§3.6）：查 `out/nom` 漂移、样本中断，给出"等效满长"吞吐 | `python3 tools/audit_run_lengths.py --scenario conc-64-deepseek` |

两张场景表都遵守仓库约定：**全部使用新 key（`v2-*`），不改动任何旧 key 的参数**，
旧 run 历史因此保持可比；`sla` 按 §6 方法标定；`extra_args: ["--ignore-eos"]` 锁死输出长度（§3.6）。

---

## 5. 怎么从数据里读出"最优解"与"安全边界"

```
0) 长度前置：确认本轮 --ignore-eos 生效（audit_run_lengths.py 中 out/nom ≈ 1.000）；
   若长度不可控，先把 tok/s 归一（等效满长）再进入下面的比较
1) 单流能力（层 A/形状 c1）→ 得到延迟下界：TPOT_min、TTFT_service
2) 层 B 速率扫描 → 画 (rate, p99 TTFT) 与 (rate, out tok/s)
     · 吞吐平台起点 r_plateau   = out tok/s 增量 < 5% 的最小 rate
     · 容量点      r_capacity  = p99 TTFT ≤ SLO 的最大 rate
     · 安全边界    r_safe      = 通过门禁（0 失败 + SLO）的最大 rate；再往上交给探针
3) 层 A 并发扫描 → 找吞吐最优并发 conc_opt（out tok/s 拐点）与 conc_max（TTFT 仍达标）
4) 决策（对齐需求三条）
     · 不崩溃：成功率 100% + 零 wrapper timeout + 无组重建（探针档除外）
     · 最优 TTFT/TPOT：在 conc ≤ conc_opt 且 rate ≤ r_capacity 内取最小延迟点
     · 最大 tok/s：取吞吐平台（当前 pernode: 3354 tok/s @ conc160/r5；稳态 2903 @ rate2）
5) 每条结论必须带：部署形态 + cap + 拓扑 + **实测长度均值 mean_out** + 重复次数
```

**当前配置下的预期答案**（供复测对照，来自 10-10 §9.3）：
`r_capacity ≈ 2 req/s`、`conc_opt ≈ 160`、稳态 `TTFT 1.05s / TPOT 14.8ms / 2903 tok/s`、
峰值 `3354 tok/s`。

> ⚠ **但"rate 2 = 稳态最优"是长度未受控时标出来的**：那一轮实际输出均值只有 1729/2048
> （§3.6），输出需求 ≈ 2×1729 = 3.5K tok/s；一旦 `--ignore-eos` 把输出锁到满长 2048，
> rate 2 的输出需求就是 **4096 tok/s > 2903 tok/s 平台**，解码侧必然排队 ——
> 即**长度受控后容量点预计下移到 ≈1.4–1.5 req/s**（`sweep` 的 r1.5 正是为它准备）。
> 所以 v2 复测若与历史 `r_capacity = 2.0` 偏离 20–30%，那是**发现而不是错误**；
> 只有当同代、同长度口径的两次结果偏离 > 10% 时，才去查 §3.4 的 cap 是否变了。

---

## 6. SLA 标定方法（别再拍值）

1. 形状内先跑一次单流能力档 → 取 `TPOT_min`；
2. 绿区档（rate ≤ 2、conc ≤ 64）跑 3 次 → 取 p99 的中位，再 **乘 1.3 余量**，得该形状的 p99 TTFT/TPOT 阈值；
3. 交互型画像（chat）收紧 TTFT（如 ≤ 3s），批处理型（rag/code/长上下文）放宽 TTFT、收紧 TPOT；
4. `min_success_rate=1.0`、`max_failed_requests=0` 保持（这条设计是对的）；
5. 边界探针不打 `gate` 标签，只打 `known-failure`；
6. 跨代回归优先用**相对阈值**（≤ 上次已知好值 +10%），绝对阈值只在同代内用；
7. **阈值只用长度受控的 run 标定**（`audit_run_lengths.py` 里 `out/nom ≈ 1.000`）；
   历史 run 若要复用，先按"等效满长"归一，并确认同为同一镜像时代（§3.6）。

---

## 6.1 已修复（本轮）与建议的后续改动

**已在本仓库完成并验证**：`campaign` 支持自定义场景文件（`--scenario-file`）、
`repeats` 真正生效、`--dry-run` 校验 key（§3.10）。

**建议（未改，等现场确认后再动）**：
| # | 建议 | 收益 |
|---|---|---|
| 1 | v2 场景文件里 `--ignore-eos` 冒烟确认后，把该 flag 固化进 `scenarios.py::_scenario()` 的默认 `extra_args` | 新场景默认长度受控，不再重演 §3.6 |
| 2 | `sglbench run` 记录 `mean_out/nominal_out`（或直接复用 `total_generated_tokens`）并写进 run 摘要 | 跨 run 比较时一眼看出长度口径是否一致 |
| 3 | `gate` 增加"长度受控"前置检查（`out/nom < 0.98` 时拒绝判定） | 门禁不被长度噪声污染 |
| 4 | `suite`/战役报告里加"实验语义"列（复用 `tools/scenario_budget.py`） | 报告自带"这份数据能不能回答问题"的自检 |

---

## 7. 运行手册

```bash
SF="--scenario-file scenarios/standard-v2.json --scenario-file scenarios/capacity-v2.json"
ENV="--env-file envs/dsv4-0731-pernode-c48.json"

# 0a) 前置：确认 cap / 拓扑 / env 与本次结论一致（cap 变了，历史数字即作废）
sglbench scenarios --scenario-file scenarios/standard-v2.json     # 看目录
sglbench preflight --scenario v2-conc-64-standard $SF $ENV

# 0b) 关键前置：确认 --ignore-eos 在本集群版本存在（§3.6）——失败就删掉各行的 extra_args
sglbench run --scenario v2-single-chat-c1 $SF $ENV --set num_prompts=8 --timeout 20m
python3 -m sglbench run --scenario v2-single-chat-c1 $SF $ENV --set num_prompts=8 --timeout 20m 2>&1 | grep -i "unrecognized"

# 0c) 开局先审计历史长度口径，确认基线数字是否可比
python3 tools/audit_run_lengths.py --scenario conc-64-deepseek
python3 tools/scenario_budget.py --scenario-file scenarios/standard-v2.json --family v2-standard

# 1) 层 A：链路健康回归（闭合环并发阶梯，一条命令）
python3 -m sglbench campaign run campaigns/v2-standard.json --side v2 $SF $ENV --exec docker

# 2) 层 B：容量点定位（开放环速率扫描，5 点共用同一 SLO）
python3 -m sglbench campaign run campaigns/v2-capacity.json --side v2 $SF $ENV --exec docker
python3 -m sglbench sweep --family v2-capacity       # 出速率-时延/吞吐曲线与拐点

# 3) 画图 + 对拍
python3 -m sglbench report --scenario v2-conc-64-standard --last 3 --sweep -o v2-layerA.html
python3 -m sglbench compare --scenario v2-conc-64-standard --last 2

# 4) 层 C：边界探针（预期失败；3h 超时 + 自愈；勿入门禁）
python3 -m sglbench campaign run campaigns/v2-probe.json --side v2 $SF $ENV --exec docker
```

> 战役 spec 内的 `repeats` 现在会真正重复执行（§3.10 修复），`--dry-run` 会先校验所有 key。

---

## 8. 已定论 / 待确认

**已定论（本轮实测核查，2026-10-12）**：
1. `random_range_ratio=1.0` 在本集群版本下**不产生长度抖动** —— 267 个 run 的 `input_len` 恒等于标称值（§3.6 更正）。
2. 输出长度不受控的真因是 **EOS 早停**（双峰分布；新镜像下 `out/nom` 0.11–1.00），
   且 10-08 下午镜像切换前后行为不同 → 修法是 `--ignore-eos` 或真实数据集。
3. 落库吞吐指标口径 = 实测生成 token ÷ bench duration（不是标称 token 也不是 wrapper wall_s）。
4. `campaign` 的 `--scenario-file` 与 `repeats` 此前静默失效，已修复并验证（§3.10）。

**仍待现场确认**：
1. **`--ignore-eos` flag 名**（本机无 sglang 源码、外网不通）—— 冒烟一次即可；不存在就删 `extra_args`。
2. **高并发下的"1-token 完成"是 EOS 还是引擎提前结束（§3.6.1）** —— 用 `--ignore-eos` 对照一次即可分流；
   若是后者，属服务正确性问题，需按 batch 分层复现。
3. **镜像切换为何改变长度行为**（10-08 下午起）：哪一项变了（引擎/模型/采样）？这决定历史 run 能否与新版对拍。
4. **`C_in ≈ 8.2K in-tok/s` 的口径**：按 bench 的实测 in tok/s 复核（4K/2K 档 rate 2 实测
   `input_token_throughput_tok_s ≈ 6.9K`，对应 offered 8.2K → 是否已是 84% 交付率）。
5. **pernode 下的绿/黄/红区**是否与 PP1 包络一致 —— `conc > 160`、`rate 8` 都还没复测（v2 探针负责钉）。
6. **GLM 侧容量点** —— 部署后再标，不要沿用 DeepSeek 的 rate/SLA（§3.9）。

---

## 附：逐行"改什么"清单

| # | 保留 | 修改 |
|---|---|---|
| ① | 形状 4096/2048、conc 1、100% 成功 SLA | n 50 → 100；加 `--ignore-eos` 锁死输出长度（§3.6）；p99 TTFT SLA 5000 → 4000ms（按 §6 重标） |
| ② | 形状 65536/8192、conc 1 | 改名去"200K"；`rate` 0.5 → 0.05–0.1（填满语义）；n 20 → 20（保留，但明白 p99 只是参考） |
| ③ | conc 32、闭合环语义 | 删"Decode 满配"因果（瓶颈由 cap 决定）；`rate` 标注为填满速率；补前置条件 cap/拓扑 |
| ④ | conc 64 | 同上；并明确它是"舒适生产水位"而非"Prefill 满配" |
| ⑤ | conc 128 这一档位本身（新代下是绿区） | 去掉 `-safe`/"安全档"命名；安全/边界改由 known-failure 探针承担（conc 256 / 65K×32） |
| ⑥ | 65K/8K 形状 | conc 32 → 8（稳定档）；32 降级为 `v2-boundary-longctx-65k-c32` 探针；`rate` 2.0 → 1.0；`--timeout 3h` |
