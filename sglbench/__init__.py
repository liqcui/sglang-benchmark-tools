"""sglbench —— SGLang PD 分离集群压测执行 / 归档 / 对比 / 可视化工具箱。

设计要点（业界通行的 benchmark 数据实践）：

1. **原始工件不可变**：每次 test run 的 stdout、命令行、环境快照、每请求明细
   全部按 ``<runs_dir>/<run_id>/`` 落盘，永不就地改写；数据库只是这些工件的
   可重建索引。
2. **分析型存储用 DuckDB**：单文件、零服务、列存，直接支持 ``read_parquet`` /
   ``read_json_auto`` 与 pandas/Arrow 互操作，比 SQLite 更适合"宽表 + 聚合 +
   分布统计"的压测分析场景。
3. **配置即元数据**：场景参数、模型、tokenizer、端点、环境变量都随 run 一起
   入库，保证任何两个 run 都可以被回答"它们到底哪里不一样"。
4. **分布优先于均值**：能拿到每请求明细时一律保存原始样本，对比时做分位数、
   KS / Mann-Whitney / bootstrap，而不是只比一个均值。
5. **离线可用**：除 DuckDB 外零第三方依赖；报告是自包含 HTML（内联 SVG），
   不引用任何 CDN，适配离线集群。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
