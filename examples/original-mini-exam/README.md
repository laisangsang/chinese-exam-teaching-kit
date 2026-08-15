# 原创微型示范卷

这是 `chinese-exam-teaching-kit` 的端到端公开示例。题面、答案、讲义和知识卡全部为本项目从零创作；青禾中学、人物和数据均为虚构。示例不复制、不改写任何第三方试题、课文、答案或教师材料。

## 包含内容

- `source/`：六板块原创题面与“本项目原创示例评分参考”。
- `content/`：一份整卷总览和六份可独立授课的 Markdown 详案。
- `knowledge/`：三张仅在示例库中使用的候选知识卡及确定性索引。
- `ORIGINALITY.md`：逐文件原创说明和 SHA-256 清单。

示例知识卡不会写入仓库根目录的公共空库；示例通过不代表公共知识内容已经完备。

## 复现

在项目根目录运行：

```bash
cekit validate --content examples/original-mini-exam/content
cekit build --content examples/original-mini-exam/content --output .local/demo-output
```

预期结果是七份 Markdown 均通过内容合同，并在 `.local/demo-output/` 生成七份 DOCX、`delivery.json` 和逐页检查清单。自动构建最多把视觉状态记为 `evidence_ready`；必须在人工逐页查看后才能宣称视觉验收通过。

## 与不同智能体协作

任何能够读取本地文件、执行非交互命令并编辑 Markdown 的智能体都可复现该示例，包括 Codex、Claude Code、WorkBuddy/CodeBuddy 或通用命令行智能体。智能体应先读取仓库的代理说明和任务生成的 `work_orders/analysis.md`，以 Markdown 为内容真源，运行校验与构建，并把二进制产物只留在 `.local/`。不同智能体不需要专有提示格式，不能把示例答案当作处理新试卷时的答案模板。
