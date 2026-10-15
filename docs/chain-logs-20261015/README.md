# 10-15 实验链日志归档（服务器下线前保全）

| 文件 | 内容 |
|---|---|
| final-unified*.log | 统一镜像终测 c64/c128/c160/c192（+rep2） |
| gap-g1/g2/g3.log | Gap 补测 G1 长ctx / G2 p96 / G3 默认档 |
| cs8k.log / cs32k.log | chunk-size 8192 / 32768（32K=prefill OOM 边界） |
| cap56*.log | 参数扫描 E1: decode cap 56（首跑+rep2） |
| restore2-3p5d.log | chunk 实验后终态恢复（16384） |
| refrozen.log | E1 回滚+锚点（20:24 服务器下线中断，锚点未跑） |
| feel-warmup.log | 保活探测全程（含 20:24+ 事件窗口） |

归档时间：2026-10-15 20:55（服务器下线前）。结论与文档索引：docs/GAP-ANALYSIS-2026-10-15.md
（附录 C/D/E）+ docs/TEST-REPORT-2026-10-13-safe-boundary-0731.md §8.8。
