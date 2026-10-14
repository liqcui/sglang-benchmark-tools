# sglbench 项目长期记忆

## 运行环境（重要，反复用到）

- **本机 managed Python 3.13 没装 duckdb/requests** → 直接跑 `unittest discover` 会得到一堆
  `ModuleNotFoundError: duckdb` 的 ERROR/FAILURE（历史上是 18F/70E），**这不是代码缺陷**。
- 已建隔离 venv 并装好依赖：
  - 路径 `C:/Users/suanova01/.workbuddy/binaries/python/envs/default/Scripts/python.exe`
  - 已装 `duckdb 1.5.6` + `requests 2.34.2`
  - **完整测试命令**：
    `cd C:/工作文件/05-代码文件/sglang-benchmark-tools && C:/Users/suanova01/.workbuddy/binaries/python/envs/default/Scripts/python.exe -m unittest discover -s tests -t .`
  - 当前基线：**303 tests OK**（约 114s）。
- 集群侧从未实跑（无 GPU / 无 lm-eval）；所有新增能力用 fake/mock 在本地验证。

## 仓库约定（改代码前必读）

- **改 `sglbench/scenarios.py` 必须重跑 `tools/gen_openclaw_skill.py`**，否则
  `tests/test_openclaw_skill.py` 会失败（SKILL.md 需与场景/CLI 同步）。
- **改 DB schema 要升 `sglbench/store.py` 的 `SCHEMA_VERSION`** 并同步 `tests/test_store.py`
  （schema 版本断言、导出表数量断言）。
- **改场景参数不要直接改旧 key**（会让 DB 里历史 run 失去可比性）；新增 key 或 `--set` 覆盖。
- `data/` 是 sglbench 家目录（含 bench.duckdb、runs/、evals/、reports/、exports/）；
  `envs/` 放版本化配置（场景覆盖、评测子集等）。

## 已建成的能力地图

- 性能：scenarios / preflight / run / ingest / list / show / compare / sweep / trend /
  gate / report / dashboard / campaign(run+report) / recover / forensics / status / mcp。
- **精度（2026-10-13 新增）**：`sglbench/quality.py` + `eval` 子命令 +
  `run --eval-after` / `campaign.eval_after` 钩子；`quality_evals` + `quality_metrics` 表。
