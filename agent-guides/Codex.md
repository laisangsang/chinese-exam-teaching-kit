# Codex 本地协作指南

Codex 官方文档说明，它会在开始工作前发现并读取 `AGENTS.md`：先加载全局范围，再从项目根到当前工作目录按层级叠加，越靠近当前目录的文件优先级越高。见 [Codex 官方 AGENTS.md 说明](https://developers.openai.com/codex/agent-configuration/agents-md)。仍建议在新任务开头明确要求复述已读取的项目规则，这有助于发现工作目录错误，也为不支持自动发现的其他智能体保留可执行引导。

`AGENTS.md` 是薄入口：它只路由到[自动流水线说明](../docs/自动流水线说明.md)、[六板块分析规范](../docs/六板块分析规范.md)和[知识库说明](../docs/知识库说明.md)，不另设学科规则。

## 建议的任务开场

```text
先读取仓库根目录的 AGENTS.md，再运行 cekit doctor --report。真实材料只可在 .local/ 中处理；禁止上传、外部 API、Git 提交和推送。读取工作单、自动流水线说明与六板块分析规范后，再按通用智能体指南完成分析、校验、构建和人工逐页验收。
```

新任务创建与恢复命令：

```bash
cekit init --name 原创练习 --exam .local/inbox/原创练习.md --answer .local/inbox/原创答案.md
cekit run --task .local/tasks/原创练习/task.json
```

若本机能力不足或入口未被正确读取，停止对真实材料的进一步处理，改为先核对 `cekit doctor --report` 和工作单；不要以猜测补足 OCR、答案或媒体内容。
