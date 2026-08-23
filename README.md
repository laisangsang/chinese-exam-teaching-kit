# Chinese Exam Teaching Kit

把高中语文试卷和本地教学材料整理成可校验、可编辑、可打印的教师讲评详案；材料默认在本地离线处理。

> **版权与隐私先行**：请只处理你有权使用的材料。真实试卷、答案、音视频、提取稿和生成的 Word 必须留在 `.local/`，不要提交到 Git，也不要上传给外部服务。本仓库只包含原创微型案例，不附带第三方试题。

## 三类入口

- **教师入口**：从[快速开始](docs/快速开始.md)照步骤完成环境检查、原创示例和自己的第一份讲评稿；不需要先理解代码。
- **智能体入口**：把[通用智能体指南](agent-guides/通用智能体指南.md)交给能读写本地文件并运行命令的智能体，再按[兼容性说明](docs/兼容性.md)选择 Codex、Claude Code 或 WorkBuddy/CodeBuddy 入口。
- **开发者入口**：阅读[架构与开发](docs/架构与开发.md)、运行测试，并从本地源码安装可编辑版本。

## 五分钟原创示例

要求 Python 3.11 或更高版本。以源码 editable install 运行原创微型示范卷，先做环境检查、内容校验和 Word 构建；完整命令与人工逐页验收见[原创示例教程](docs/原创示例教程.md)和下方的详细流程。

## 能力边界

材料默认本地离线处理；项目不上传材料、不调用外部大模型、不下载模型，也不自动 Git 提交或发布。扫描 PDF OCR、媒体转写和页面渲染依赖本机能力，自动构建不代替人工逐页验收；详见[完整安装说明](docs/完整安装说明.md)和[Word 生成与版式验收](docs/Word生成与版式验收.md)。

## 支持矩阵

核心 CLI、文字层提取和 Word 构建支持 macOS、Windows、Linux；macOS 可选 Apple Vision OCR，其他系统由本地 provider 注入。各智能体入口及版本核验边界见[兼容性说明](docs/兼容性.md)。

## 项目状态与贡献

公开仓库已发布不可变的 `v0.1.0` Release；当前 `main` 继续承载发布后加固，不改写旧 tag 或资产。项目尚未发布 PyPI，请从公开 GitHub 源码安装。公共知识库是空框架，不代表历史知识完备。

## 完整安装与详细流程

要求 Python 3.11 或更高版本。从公开仓库 `https://github.com/laisangsang/chinese-exam-teaching-kit.git` 克隆，再从源码安装；尚不要将 PyPI 当作发布渠道。

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

下面的示例假定你已将有权使用的文件放在未跟踪的 `.local/inbox/`。对 Markdown、TXT、DOCX 和 PDF，优先用可重复的 `--exam` 与 `--answer` 显式绑定角色；`--input` 仍用于需要自动分类的材料。

```bash
cekit init --name 青禾原创练习 --exam .local/inbox/青禾原创练习.md --answer .local/inbox/青禾原创答案.md
cekit run --task .local/tasks/青禾原创练习/task.json
cekit status --task .local/tasks/青禾原创练习/task.json --json
```

第一次 `run` 会先在 `knowledge_pre` 等待逐题 `question-manifest.json`；满足题号、来源 SHA-256 和提取索引的精确覆盖后，才会生成真实检索收据并进入分析工作单。分析后还须在 `post-review.json` 逐题裁决冲突与候选知识；缺证据时阶段保持 `waiting`。

```bash
cekit validate --task .local/tasks/青禾原创练习/task.json
cekit run --task .local/tasks/青禾原创练习/task.json
```

也可以对独立内容目录构建：

```bash
cekit build --content .local/tasks/青禾原创练习/content --output .local/tasks/青禾原创练习/manual-output
```

完整的暂停、恢复和后补答案边界见[自动流水线说明](docs/自动流水线说明.md)。

## 详细能力边界

- 支持 Markdown、纯文本、DOCX 和 PDF 的本地文字提取；扫描型 PDF 需要本地 OCR 条件。
- macOS 可选用随包提供的 Apple Vision OCR；其他系统需要注入本地 `OcrProvider`。项目不会自动下载模型。
- 媒体学习需要调用方注入本地转写器；缺少转写或画面工具时会降级并继续试卷流程。
- 自动流水线不调用外部大模型 API，不自动上传材料，不自动执行 Git 提交或推送。
- 分析写作由教师或智能体完成，系统提供工作单、内容合同、确定性校验和 Word 构建，不保证自动生成学科结论。
- `evidence_ready` 不等于人工视觉验收 `passed`；当前 CLI 不提供伪造人工签名的捷径。

## 详细支持矩阵

| 能力 | macOS | Windows | Linux |
| --- | --- | --- | --- |
| 核心 CLI、Markdown/DOCX/PDF 文字层、Word 构建 | 支持 | 支持 | 支持 |
| 扫描 PDF OCR | Apple Vision 可选；亦可注入本地实现 | 需注入本地实现 | 需注入本地实现 |
| PDF 页面渲染 | 本地 Poppler 可增强 | 本地 Poppler 可增强 | 本地 Poppler 可增强 |
| 音视频章节学习 | 需 FFmpeg 与注入式本地转写器 | 同左 | 同左 |
| Codex / Claude Code / WorkBuddy-CodeBuddy | 提供项目入口或指南；具体版本仍需验证 | 同左 | 同左 |

环境事实以 `cekit doctor` 的本机报告为准，完整安装选项见[完整安装说明](docs/完整安装说明.md)，智能体支持等级见[兼容性](docs/兼容性.md)。

## 详细项目状态与贡献

已发布 `v0.1.0`；当前 `main` 为发布后加固状态。公共知识库是可用的空框架，不代表已有历史知识内容；参见[知识库说明](docs/知识库说明.md)。常见问题见[FAQ](docs/常见问题.md)。

欢迎在确保内容原创、来源可追溯、无私有材料的前提下参与。开发前请阅读[架构与开发](docs/架构与开发.md)，提交前运行：

```bash
cekit release-audit
```

许可证为 Apache-2.0，详情见 [LICENSE](LICENSE) 与 [NOTICE](NOTICE)。
