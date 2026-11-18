# Meridian 区域媒体证据分析服务

本服务供研究团队对本地提供的媒体观察记录进行地区、议题和受众比较。系统不抓取网页、不自动推断报道倾向，也不声称合成演示能代表真实群体。

来源登记绑定地区、媒体和允许词表；观察记录按来源和外部编号保留修订。两名研究员独立编码，分歧由第三人裁定。按地区、故事、议题和受众去重，报告显示分母、排除原因和原始证据数。

采集覆盖按来源登记窗口承诺（`expected_count`）。报告和地区对比区分两类信息：已确认的故事（有编码、可复现）与尚未到齐的来源覆盖（承诺数 − 有效观察数）。缺口只作为缺口呈现：

- `report["coverage"]`、`report["provisional"]`、`report["coverage_status"]` 和每个地区的 `stance_bounds` 给出覆盖状态与可能区间，未收到的报道不分配倾向。
- `POST /reports/compare-regions` 返回 `tentative`、`sensitivity`（哪些结论可能因缺口翻转）和 `trace`（地区 → 来源 → 窗口 → 故事分母）。
- `POST /reports/coverage` 是研究员的覆盖追溯入口；`source_breakdown` 与导出包同样携带覆盖状态。
- 窗口可以绑定批次（`batch_id`）；批次未走完或有拒绝行时窗口不能封存，已接受行和待处理行不能合成一个完整窗口。
- 窗口封存后若原记录更正或撤回，会重新出现差额（`drift_kind` 为 `revised`/`shortfall`/`surplus`）。`POST /frames/{frame_id}/reseal` 只能在仍满足原承诺条数时重新封存；`POST /frames/{frame_id}/reopen` 退回未封存状态以便重新导入。
- 已发布报告的覆盖解释被冻结；`/releases/{release_id}/compare` 同时报告当前覆盖变化。缺口未补齐时预览和发布会被 `collection_coverage`/`collection_gap` 阻止。

使用 `python -m meridian.api --db meridian.sqlite3` 启动 FastAPI。主要接口包括 `POST /sources`、`POST /observations`、`POST /decisions`、`POST /adjudications`、`POST /batches`、`POST /reports` 和 `POST /releases`。采集窗口使用 `/frames`，导出使用 `/reports/export` 或 `/releases/{release_id}/export`。

运行 `python -m unittest discover -s tests -v`、`python -m compileall -q meridian` 和 `python -m meridian.demo`。数据库使用 SQLite；测试和演示使用临时本地数据库。
