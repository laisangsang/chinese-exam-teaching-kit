# 本地教学任务入口

真实材料只在 `.local/` 中处理；禁止上传、外部 API、自动 Git 提交或推送。开始前运行 `cekit doctor --report`，并读取任务的 `work_orders/analysis.md`。

唯一流程规范是 [docs/自动流水线说明.md](docs/自动流水线说明.md)；唯一学科分析规范是 [docs/六板块分析规范.md](docs/六板块分析规范.md)。知识状态与审计按 [docs/知识库说明.md](docs/知识库说明.md)。正式 Markdown 写入任务 `content/`，公共 `knowledge/` 不能放真实材料。

详案严格区分【官方评分参考】【文本推导】【教学拓展】。完成后运行校验和构建，并按 [docs/Word生成与版式验收.md](docs/Word生成与版式验收.md)人工逐页复核；`evidence_ready` 不等于视觉验收通过。
