# WorkBuddy 与腾讯 CodeBuddy 指南

## WorkBuddy：手动参考入口

在 WorkBuddy 中打开本地仓库，并手动引用本文件、`AGENTS.md` 和[通用智能体指南](通用智能体指南.md)。当前只将 WorkBuddy 作为可手动参考的协作方式：未核验其是否自动读取项目入口、支持何种规则格式，或提供可原生安装的 Skill。不要把本指南描述为 WorkBuddy 的原生 Skill。

## 腾讯 CodeBuddy：项目规则入口

仓库含有 `.codebuddy/rules/project/RULE.mdc`，用于把项目规则路由回 `AGENTS.md` 和两份唯一规范。腾讯官方文档说明项目规则目录的用法，见[项目规则文档](https://cloud.tencent.com/document/product/1831/134362)。实际客户端版本是否自动加载规则、何时生效，须在本机创建一个无敏感内容的测试任务验证；不能因目录存在就夸大兼容性。

## 共同执行方式

无论使用哪个工具，都先运行：

```bash
cekit doctor --report
```

再按下列顺序执行：

1. 仅将真实材料放入 `.local/inbox/`，创建本地任务并运行到分析暂停点。
2. 阅读任务 `work_orders/analysis.md`、[自动流水线说明](../docs/自动流水线说明.md)和[六板块分析规范](../docs/六板块分析规范.md)。
3. 按【官方评分参考】【文本推导】【教学拓展】分层写入任务 `content/`，完成知识审计。
4. 校验、恢复运行，并人工逐页查看本地 DOCX 产物。详见[Word 生成与版式验收](../docs/Word生成与版式验收.md)。

```bash
cekit init --name 原创练习 --input .local/inbox/原创练习.md
cekit run --task .local/tasks/原创练习/task.json
cekit validate --task .local/tasks/原创练习/task.json
```

不得上传材料、调用外部大模型 API、自动提交或推送，也不得在媒体能力降级时捏造视频或音频结论。
