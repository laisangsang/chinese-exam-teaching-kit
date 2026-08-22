# Chinese Exam Teaching Kit

把高中语文试卷和本地教学材料整理成可校验、可编辑、可打印的教师讲评详案；材料默认在本地离线处理。

> **版权与隐私先行**：请只处理你有权使用的材料。真实试卷、答案、音视频、提取稿和生成的 Word 必须留在 `.local/`，不要提交到 Git，也不要上传给外部服务。本仓库只包含原创微型案例，不附带第三方试题。

## 三类入口

- **教师入口**：从[快速开始](docs/快速开始.md)照步骤完成环境检查、原创示例和自己的第一份讲评稿；不需要先理解代码。
- **智能体入口**：把[通用智能体指南](agent-guides/通用智能体指南.md)交给能读写本地文件并运行命令的智能体，再按[兼容性说明](docs/兼容性.md)选择 Codex、Claude Code 或 WorkBuddy/CodeBuddy 入口。
- **开发者入口**：阅读[架构与开发](docs/架构与开发.md)、运行测试，并从本地源码安装可编辑版本。

## 五分钟体验原创示例

要求 Python 3.11 或更高版本。当前公开发行准备阶段以本地源码安装为准；仓库正式发布后可从授权目标地址 `https://github.com/laisangsang/chinese-exam-teaching-kit.git` 克隆。

```bash
python -m venv .venv
```

macOS / Linux：

```bash
source .venv/bin/activate
python -m pip install -e .
```

Windows PowerShell：

```powershell
.venv\Scripts\Activate.ps1
python -m pip install -e .
```

在仓库根目录运行：

```bash
cekit doctor --report
cekit validate --content examples/original-mini-exam/content
cekit build --content examples/original-mini-exam/content --output .local/demo-output
```

你会得到七份 DOCX、`delivery.json` 和 `visual-review-checklist.md`。这只表示内容校验通过且视觉证据待检查，必须人工渲染、人工逐页查看后才能称为版式验收通过。详见[原创示例教程](docs/原创示例教程.md)和[Word 生成与版式验收](docs/Word生成与版式验收.md)。

## 处理自己的材料

下面的示例假定你已将有权使用的文件放在未跟踪的 `.local/inbox/`。`--input` 可重复，用于同一任务的试卷、答案等文件。

```bash
cekit init --name 青禾原创练习 --input .local/inbox/青禾原创练习.md --input .local/inbox/青禾原创答案.md
cekit run --task .local/tasks/青禾原创练习/task.json
cekit status --task .local/tasks/青禾原创练习/task.json --json
```

第一次 `run` 通常会暂停在分析阶段。请读取 `.local/tasks/青禾原创练习/work_orders/analysis.md`，按[六板块分析规范](docs/六板块分析规范.md)编写 `content/` 中的 Markdown，然后：

```bash
cekit validate --task .local/tasks/青禾原创练习/task.json
cekit run --task .local/tasks/青禾原创练习/task.json
```

也可以对独立内容目录构建：

```bash
cekit build --content .local/tasks/青禾原创练习/content --output .local/tasks/青禾原创练习/manual-output
```

完整的暂停、恢复和后补答案边界见[自动流水线说明](docs/自动流水线说明.md)。

## 能力边界

- 支持 Markdown、纯文本、DOCX 和 PDF 的本地文字提取；扫描型 PDF 需要本地 OCR 条件。
- macOS 可选用随包提供的 Apple Vision OCR；其他系统需要注入本地 `OcrProvider`。项目不会自动下载模型。
- 媒体学习需要调用方注入本地转写器；缺少转写或画面工具时会降级并继续试卷流程。
- 自动流水线不调用外部大模型 API，不自动上传材料，不自动执行 Git 提交或推送。
- 分析写作由教师或智能体完成，系统提供工作单、内容合同、确定性校验和 Word 构建，不保证自动生成学科结论。
- `evidence_ready` 不等于人工视觉验收 `passed`；当前 CLI 不提供伪造人工签名的捷径。

## 支持矩阵

| 能力 | macOS | Windows | Linux |
| --- | --- | --- | --- |
| 核心 CLI、Markdown/DOCX/PDF 文字层、Word 构建 | 支持 | 支持 | 支持 |
| 扫描 PDF OCR | Apple Vision 可选；亦可注入本地实现 | 需注入本地实现 | 需注入本地实现 |
| PDF 页面渲染 | 本地 Poppler 可增强 | 本地 Poppler 可增强 | 本地 Poppler 可增强 |
| 音视频章节学习 | 需 FFmpeg 与注入式本地转写器 | 同左 | 同左 |
| Codex / Claude Code / WorkBuddy-CodeBuddy | 提供项目入口或指南；具体版本仍需验证 | 同左 | 同左 |

环境事实以 `cekit doctor` 的本机报告为准，完整安装选项见[完整安装说明](docs/完整安装说明.md)，智能体支持等级见[兼容性](docs/兼容性.md)。

## 项目状态与贡献

当前版本为 `0.1.0` 本地预览，正在准备首次公开发行。公共知识库是可用的空框架，不代表已有历史知识内容；参见[知识库说明](docs/知识库说明.md)。常见问题见[FAQ](docs/常见问题.md)。

欢迎在确保内容原创、来源可追溯、无私有材料的前提下参与。开发前请阅读[架构与开发](docs/架构与开发.md)，提交前运行：

```bash
cekit release-audit
```

许可证为 Apache-2.0，详情见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。
