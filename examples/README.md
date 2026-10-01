# 示例数据（examples/）

本目录下的数据是**纯合成的示例**，用于演示/回归测试「SGLang PD 分离集群压测」的产物格式，
不是真实跑出来的 benchmark 结果。所有文件都由 `tools/make_examples.py` 以固定随机种子生成，
重复运行结果完全一致。

## 目录结构

```
examples/
├── README.md                       # 本文件
└── logs/
    ├── <name>.log                  # 模拟 python3 -m sglang.bench_serving 的 stdout
    ├── <name>.requests.jsonl       # 该 run 的每请求明细，每行一个 JSON 对象
    └── second-run/
        ├── README.md
        ├── single-standard__second.log
        └── single-standard__second.requests.jsonl
```

命名规则：`<scenario_key>__<label>.log`。同一个 `scenario_key` 下的多个 `label` 属于同一场景的多次 run
（例如升级固件前后的 baseline / candidate），可以直接横向对比。

## 两批数据

**批次一（`logs/` 根目录）—— 场景对比演示**

| name | 场景特征 | 用途 |
|---|---|---|
| `single-standard__baseline` | c=1, n=50, in=4096, out=2048, rr=1.0 | 单并发标准长度基准 |
| `single-standard__candidate` | 同上，第二次 test run | 模拟固件/驱动升级：TTFT +10% 回归，TPOT +1%（噪声） |
| `single-longctx__baseline` | c=1, n=20, in=65536, out=8192, rr=0.5 | 长上下文：TTFT、TPOT 显著变慢 |
| `conc-32-standard__baseline` | c=32, n=200, in=4096, out=2048, rr=4.0 | 中等并发 |
| `conc-64-standard__baseline` | c=64, n=200, in=4096, out=2048, rr=6.0 | 高并发 |
| `conc-128-standard__baseline` | c=128, n=300, in=4096, out=2048, rr=8.0 | **崩溃场景**：300 提交 / 212 成功 / 88 失败（OOM、prefill worker 重启、超时） |
| `prefill-only__baseline` | host `10.66.1.232:8000`, backend=sglang, c=64, n=100, in=8192, out=128, rr=2.0 | prefill 节点单独压测（极短输出） |
| `decode-only__baseline` | host `10.66.1.166:8000`, backend=sglang, c=32, n=100, in=2048, out=2048, rr=5.0 | decode 节点单独压测 |

**批次二（`logs/second-run/`）—— 多 run 对比补充数据**

`single-standard__second` 是 `single-standard` 场景的第三次 run，TTFT 均值与 baseline 基本一致，
用于演示「重复运行无显著差异」的对照组。

## 数据格式

### `.log`

逐行格式与真实 `bench_serving` 输出一致：

1. 第一行 `Namespace(...)`，记录本次 run 的全部参数（`--base-url` 或 `--host/--port`）；
2. （仅崩溃 run）若干行服务端 stderr 混入文本，含 `torch.OutOfMemoryError` 与 pod 重启告警；
3. 一行 tqdm 进度条（`100%|████...| 50/50 [01:23<00:00, 1.67s/it]`）；
4. `============ Serving Benchmark Result ============` 结果块，标签左对齐到第 41 列，
   数值右对齐，延迟类保留 2 位小数、吞吐保留 2 位小数、token 数为整数。

结果块中包含 `Successful requests`、`Benchmark duration (s)`、token 总数、吞吐、
`Concurrency`，以及 E2E / TTFT / TPOT / ITL 四组 mean/median/P90/P95/P99。

### `.requests.jsonl`

每行一个 JSON 对象，键固定：

```json
{"request_id": "0", "input_len": 4096, "output_len": 2048, "ttft_ms": 82.13, "tpot_ms": 7.65, "itl_ms": 7.70, "e2e_ms": 1652.1, "status": "ok", "error": null}
```

* `request_id`：从 `"0"` 开始、按提交顺序递增的字符串（崩溃 run 的失败请求按发生时刻散布在中间，
  不是集中在末尾）。
* `status`：`"ok"` 或 `"error"`；失败行 `error` 为一句错误消息，`tpot_ms`/`itl_ms` 为 `null`，
  `output_len` 为已生成的部分 token 数，`e2e_ms` 仍有值。
* 自洽关系：`e2e_ms ≈ ttft_ms + tpot_ms * (output_len - 1)`（生成时按此构造，噪声 ≤ 2%）。

## 怎么用

### 0. 用 sglbench 一键跑通（推荐）

```bash
# 从仓库根目录执行；文件名 <scenario_key>__<label>.log 会被自动推断
sglbench ingest examples/logs --infer      # 递归导入，含 second-run/
sglbench list
sglbench compare --scenario single-standard --last 2
sglbench sweep   --family concurrency
sglbench report  --scenario single-standard --last 2 --sweep -o report.html
sglbench gate    --scenario conc-128-standard     # 崩溃场景应判失败，退出码 1
```

`sglbench ingest` 按内容 sha256 去重，重复导入同一份日志会直接跳过。
本目录下的 `report-sample.html` 就是上面 report 命令的输出，可直接用浏览器打开。

### 1. 直接解析日志做回归对比

对每个 `.log`，先用正则取 `Namespace(...)` 里的参数，再取结果块的标签/数值对：

```python
import re, pathlib

ROW = re.compile(r"^(?P<label>[A-Za-z][^:]*?):[ ]+(?P<value>.+)$", re.M)
for path in sorted(pathlib.Path("examples/logs").rglob("*.log")):
    text = path.read_text(encoding="utf-8")
    block = text.split("Serving Benchmark Result", 1)[1]
    stats = {m["label"].strip(): m["value"] for m in ROW.finditer(block)}
    print(path.name, stats["Mean TTFT (ms)"], stats["Mean TPOT (ms)"])
```

### 2. 从明细重新计算统计量并与日志对账

`.log` 结果块里的**每一个统计量都由同目录 `.requests.jsonl` 中的成功样本重新计算得到**
（延迟统计与 token 总量只统计 `status == "ok"` 的行，与真实 `bench_serving` 一致；
`Successful requests` 体现失败计数）：

```python
import json, numpy as np, pathlib

for jl in sorted(pathlib.Path("examples/logs").rglob("*.requests.jsonl")):
    rows = [json.loads(l) for l in jl.read_text(encoding="utf-8").splitlines() if l.strip()]
    ok = [r for r in rows if r["status"] == "ok"]
    ttft = np.array([r["ttft_ms"] for r in ok])
    e2e = np.array([r["e2e_ms"] for r in ok])
    duration = e2e.max() / 1000 * 1.05 + 1.0
    print(jl.name, len(ok), ttft.mean(), np.percentile(ttft, 99, method="linear"),
          sum(r["output_len"] for r in ok) / duration)
```

### 3. 重新生成

```bash
python tools/make_examples.py
```

脚本只依赖标准库 + numpy，确定性输出（固定 seed），运行结束会打印每个文件的路径与行数。
生成的文件均为 UTF-8 无 BOM、LF 换行。

## 已知取舍

1. **失败请求不计入延迟统计，也不计入 token 吞吐分子**。真实 `bench_serving` 的
   `input_lens`/`output_lens` 会累计所有收到的请求（含后续失败的），因此它的 token 总数可能略高。
   这里为了让 `.log` 与 `.requests.jsonl` 能逐位精确对账，统一只统计成功请求；受影响最大的只有
   `conc-128-standard__baseline`（崩溃 run，300 提交 / 212 成功）。
   日志里 `Successful requests = 212` 已经体现了失败计数，但它与 `Total input tokens` 并非同源，
   单独拿这两个数相除无法还原 `random_input`。
2. `Concurrency` 使用 `成功请求数 / Benchmark duration`（bench_serving 的吞吐定义），
   而不是压测机配置的并发上限。
3. `Benchmark duration (s) = max(e2e_ms)/1000 * 1.05 + 1.0`，即最慢请求耗时 + 约 5% 调度开销 + 1s 启停开销。
   这一定义下 `Concurrency` 会**明显低于**日志里的 `Max request concurrency`（后者只是压测机的并发上限，
   而本数据是「固定请求总数」的 closed-loop 压测，请求速率由模型计算耗时决定）。真实
   `bench_serving` 以整个 test 的实测 wall time 作 duration，因此它的 `Concurrency` 才会贴近上限。
4. 崩溃 run 的失败请求也写了 `.requests.jsonl` 行，其 `e2e_ms` 可能大于成功请求的最大值
   （挂住的请求更慢），但 duration 只由成功请求决定，以保持与吞吐自洽。
5. 各场景的 `TTFT` / `TPOT` 的 P99/Mean 按并发标定在 1.4~1.8（并发越高越接近 1.8）。
   `E2E` 的 P99/Mean 是 `e2e = ttft + tpot * (out_len - 1)` 的结果，`tpot * (out_len - 1)`
   这一项近似常量、把分布右侧拉平，所以 `E2E` 的尾比通常低于同场景的 `TTFT`：
   本批数据实测 `E2E` P99/Mean 约 1.13（c=1）~ 1.61（c=64），符合右偏但被常量项稀释的形态。
