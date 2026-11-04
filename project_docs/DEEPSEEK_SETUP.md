# DeepSeek 官方配置

核对日期：2026-11-03。现在即可配置，不必等代码全部完成。密钥只在本地应用输入。

1. 打开应用，进入“AI 工具 → AI 控制台 → LLM 控制台”；新版作品库和审校页左侧均有“AI 控制台”入口。
2. 新增配置，或导入 DeepSeek 预设。
3. 协议选 OpenAI 兼容；Base URL 填 `https://api.deepseek.com`。
4. 填入本人的 API Key，模型名填 `deepseek-flash`，或以账户拉取到的模型列表为准。
5. 开启 Chat Completions；默认 max_tokens 建议先设 8192。本次 AI 审校还会单独限制输出，默认 4096。
6. 保存配置，点击“设为启用”。新版连接测试只请求少量输出，不使用原来的大输出默认值。
7. 返回审校页重新加载；模型显示“已配置”后可以发起深度审校。是否实际可用仍需请求成功验证。

新版 DeepSeek 预设对简短审校默认关闭思考模式，避免把有限输出都用于推理；高级参数中明确设置 thinking 或 reasoning_effort 时尊重该设置。

DeepSeek 聊天模型配置与嵌入模型配置分开。本地 BM25 检索无需嵌入 API；不必为先跑通深度审校配置第二个服务。

官方依据：[首次调用与当前模型名](https://api-docs.deepseek.com/)、[Chat Completions 参数](https://api-docs.deepseek.com/api/create-chat-completion/)。本项目不会把模型别名永久视为不变，后续应以官方文档或账户模型列表为准。
