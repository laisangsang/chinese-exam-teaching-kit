# Claude Code 本地协作指南

Claude Code 支持项目记忆文件；其官方说明见[Memory 文档](https://code.claude.com/docs/en/memory)。本仓库的 `CLAUDE.md` 首行引用 `AGENTS.md`，使项目入口复用同一套薄路由，而不是复制学科合同。

打开本地仓库后，仍应在任务中明确：先读取 `CLAUDE.md` 和 `AGENTS.md`，再读取本次任务工作单、[自动流水线说明](../docs/自动流水线说明.md)及[六板块分析规范](../docs/六板块分析规范.md)。将[通用智能体指南](通用智能体指南.md)的提示词作为任务约束。

## 推荐流程

```bash
cekit doctor --report
cekit init --name 原创练习 --input .local/inbox/原创练习.md
cekit run --task .local/tasks/原创练习/task.json
```

只在 `.local/` 中处理真实材料；不要上传对话附件、调用外部 API、下载模型、自动提交或推送。分析阶段完成后，按工作单写 `content/`，然后运行：

```bash
cekit validate --task .local/tasks/原创练习/task.json
cekit run --task .local/tasks/原创练习/task.json
```

生成 DOCX 后还需要人工逐页验收，详见[Word 生成与版式验收](../docs/Word生成与版式验收.md)。Claude Code 对本项目入口的支持不改变题面、正式答案和证据分层的优先级。
