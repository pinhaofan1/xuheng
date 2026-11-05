# 开发进度维护

用户要求维护开发进度表，并在后续开发完成时自动勾选。

- 开始本项目的后续开发前，阅读 `project_docs/ROADMAP.md` 与 `project_docs/DEVELOPMENT_PROGRESS.json`，选择对应的稳定编号。
- 用户只说“继续开发”时，默认推进 `next_order` 中尚未完成的首项；用户明确选择的任务优先。人工标注或外部条件未就绪时保持未勾选，并推进其他独立工作。
- 开始落实某项工作时，运行 `python scripts/sync_progress.py --start 编号`。一次工作可只完成其中明确的一部分；必要时拆成有独立验收标准的新条目。
- 功能实现、相关检查通过且满足该项完成标准后，在本轮结束前运行 `python scripts/sync_progress.py --complete 编号 --evidence 仓库内验收记录路径`。同时提交数据和生成的表格，并在交付记录中说明验证范围。
- 尚未完成的人工标注、真实模型长跑、外部环境验证等保持未勾选。仅有部分实现或不相关的测试通过不能完成勾选。
- `project_docs/DEVELOPMENT_PROGRESS.json` 是进度状态的唯一数据源；不要直接修改生成的 `project_docs/ROADMAP.md`。新增需求先给出独立编号、优先级和完成标准。
- 发布新版本时同步更新进度数据中的版本与验证基线。
- 本项目交付前运行 `python scripts/sync_progress.py --check`。进度维护属于日常开发流程，无需另行请求用户确认。
