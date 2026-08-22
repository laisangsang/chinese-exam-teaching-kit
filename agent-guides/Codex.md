# Codex 本地协作指南

Codex 的官方材料未确认会自动读取本项目的 `AGENTS.md`。因此每次新任务都应明确要求：“先读取仓库根目录的 `AGENTS.md`，再开始处理。”随后把本项目路径作为工作目录，并使用[通用智能体指南](通用智能体指南.md)的可复制提示词。

`AGENTS.md` 是薄入口：它只路由到[自动流水线说明](../docs/自动流水线说明.md)、[六板块分析规范](../docs/六板块分析规范.md)和[知识库说明](../docs/知识库说明.md)，不另设学科规则。Codex 产品资料入口见[OpenAI 开发者文档](https://developers.openai.com/)；该链接不构成自动加载 `AGENTS.md` 的承诺。

## 建议的任务开场

```text
先读取仓库根目录的 AGENTS.md，再运行 cekit doctor --report。真实材料只可在 .local/ 中处理；禁止上传、外部 API、Git 提交和推送。读取工作单、自动流水线说明与六板块分析规范后，再按通用智能体指南完成分析、校验、构建和人工逐页验收。
```

新任务创建与恢复命令：

```bash
cekit init --name 原创练习 --input .local/inbox/原创练习.md
cekit run --task .local/tasks/原创练习/task.json
```

若本机能力不足或入口未被正确读取，停止对真实材料的进一步处理，改为先核对 `cekit doctor --report` 和工作单；不要以猜测补足 OCR、答案或媒体内容。
