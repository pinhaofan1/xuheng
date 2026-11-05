# 叙衡 Xuheng

面向长篇小说的 AI 创作与一致性审校工作台。管理作品、整理人物与世界观，检索历史章节，对照原文与依据核查疑似矛盾。

当前版本：**0.10.0 · 本机浏览器版**。技术栈：Python、FastAPI、SQLite、Vue 3、TypeScript。

![叙衡审校工作台](project_docs/screenshots/hybrid-review-04.png)

## 功能

| 模块 | 能做什么 |
| --- | --- |
| 作品库 | 新建、搜索、管理作品，查看章节及字数，进入创作或审校 |
| 章节创作 | 编辑与保存正文、辅助生成、按章节范围托管连写、查看生成进度 |
| 人物与世界观 | 管理人物档案、作品设定、世界观、知识库和人物/地点关系图 |
| 写作辅助 | 伏笔账本、故事演进、提示词管理，以及张力、文风等分析入口 |
| 一致性审校 | 本地规则检查与 AI 深度审校，对照人物事实和历史正文识别疑似矛盾 |
| 历史检索 | BM25、关键词与向量混合召回、RRF 合并、Reranker 排序及向量缓存 |
| 证据核对 | 原文精确高亮、设定/前章引用、无效引用警告，正文或依据变化后标记过期 |
| 人工处理 | 确认、忽略、撤销处理，保留反馈与运行历史，导出 JSON 审校报告 |
| 模型控制 | OpenAI 兼容聊天档案、Embedding / Reranker 配置、连接测试与实际用量记录 |
| 任务与预算 | 持久化审校任务、重启中断识别、流式实际 token 结算、累计/按日预算、模型费用汇总及账单核对历史 |
| 托管恢复 | 正文读回确认、生成稿副本与下载、按正文版本复用审计步骤、章后失败停机与显式恢复 |
| 章后任务 | 保存与托管入口共用持久化任务、模型结果留存、图谱/伏笔/债务写入幂等；未知计费请求确认后再处理 |
| 数据备份 | 全作品与运行账本联合快照、下载及上传校验、离线恢复、安全副本和恢复中断回滚 |
| 文稿导出 | EPUB、PDF、DOCX、Markdown |

## 运行

已验证环境：Windows、Python 3.13、Node.js 24。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -c requirements-tested.txt
cd frontend
npm ci
npm run build
cd ..
.\start-xuheng.ps1 -Demo
```

打开 [本地作品库](http://127.0.0.1:8005/)。准备好依赖后也可双击 `start-xuheng.cmd`，服务就绪后自动打开浏览器。

本地规则和关键词检索无需 API Key。聊天模型在左侧“AI 控制台”配置；Embedding 与 Reranker 在“检索模型”配置。密钥只在本机填写，个人数据、日志和模型凭据不随源码提交。

## 验证与范围

- 本地全量单元测试 1799 项通过、7 项跳过；15 项浏览器场景通过，验证范围见 [0.10.0 交付记录](project_docs/RELEASE_0.10.md)。
- 已真实联调 DeepSeek 审校、硅基流动 Embedding / Reranker，以及一次受控单章托管请求。
- 已修复全托管未启动执行进程却显示运行的问题，在实际作品上验证宏观规划、首幕规划、首章保存、章后流程及保护章数自动停止。早期审校联调使用虚构示例；这些检查只说明对应集成链路可用，长期托管稳定性、真实长篇质量和生产并发仍需专项验证。
- 历史检索当前限定同作品前置章节，最多最近 100 章、50 万字符。
- 真实两章受控管线已验证生成、保存和预算拦截；另有离线限流及手动恢复检查。该检查预置规划并关闭额外章后 AI 调用，不代表完整无人值守长跑。
- 任务与用量入口位于工作台“更多工具”。金额按自填单价估算；未知用量保留预留值，未配置单价保持费用未知。当前按单个本地 API 进程使用。
- 当前没有独立原生安装包或公网多用户服务。

## 文档

- [快速运行与使用](project_docs/GETTING_STARTED.md)
- [0.10.0 交付记录](project_docs/RELEASE_0.10.md)
- [架构与 API](project_docs/ARCHITECTURE.md)
- [验证记录](project_docs/VALIDATION.md)
- [开发进度表与待办清单](project_docs/ROADMAP.md)
- [模块与来源](project_docs/CONTRIBUTIONS.md)

## 许可

适用 **Apache 2.0 + Commons Clause**，完整条款见 [LICENSE](LICENSE)，代码来源与版权说明见 [NOTICE.md](NOTICE.md)。
