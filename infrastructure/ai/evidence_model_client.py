"""Bounded OpenAI-compatible chat adapter for evidence review.

Reuses the locally activated model profile. No hidden model substitution, SDK
retries or key exposure. This intentionally avoids the upstream 120k token
minimum in GenerationConfig for short, bounded review requests.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from application.audit.evidence_models import text_hash
from application.audit.services.evidence_review_service import ReviewUnavailable
from infrastructure.ai.url_utils import normalize_openai_base_url


@dataclass(frozen=True)
class ModelReply:
    content: str
    model: str
    input_tokens: int | None
    output_tokens: int | None


class EvidenceModelClient:
    def __init__(self, profile, client_factory=AsyncOpenAI):
        if profile is None or not profile.api_key.strip() or not profile.model.strip():
            raise ReviewUnavailable("请先在 AI 控制台填写并激活接口地址、API Key 和模型名")
        if profile.protocol != "openai":
            raise ReviewUnavailable("当前深度审校支持 OpenAI 兼容协议，请在 AI 控制台切换档案")
        self.profile = profile
        self.client_factory = client_factory
        self.model = profile.model
        # Configuration identity intentionally excludes API keys and headers.
        self.fingerprint = text_hash(f"{profile.id}:{profile.model}:{profile.base_url}")

    async def complete(self, system: str, user: str, output_tokens: int = 4096) -> ModelReply:
        profile = self.profile
        token_cap = min(8192, max(1, output_tokens))
        # Gateways may explicitly request the newer token parameter via config.
        parameter = "max_completion_tokens" if "max_completion_tokens" in profile.extra_body else "max_tokens"
        body = {k: v for k, v in profile.extra_body.items()
                if k not in {"max_tokens", "max_completion_tokens", "messages", "model", "stream", "n", "tools", "tool_choice", "response_format"}}
        if urlsplit(profile.base_url).hostname == 'api.deepseek.com' and 'thinking' not in body and 'reasoning_effort' not in body:
            # DeepSeek enables thinking by default. A short, explicitly bounded
            # request should not spend its whole output budget on reasoning.
            body['thinking'] = {'type': 'disabled'}
        kwargs = {
            "model": profile.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            parameter: token_cap, "stream": False,
        }
        # JSON mode is opt-in for compatible gateways; local schema validation is
        # always mandatory. Unsupported parameters are reported, not retried.
        if profile.extra_body.get("response_format"):
            kwargs["response_format"] = profile.extra_body["response_format"]
        if body:
            kwargs["extra_body"] = body
        if profile.extra_headers:
            kwargs["extra_headers"] = profile.extra_headers
        if profile.extra_query:
            kwargs["extra_query"] = profile.extra_query
        try:
            async with httpx.AsyncClient(timeout=min(profile.timeout_seconds, 90), trust_env=False) as transport:
                async with self.client_factory(
                    api_key=profile.api_key, base_url=normalize_openai_base_url(profile.base_url) or None,
                    max_retries=0, http_client=transport,
                ) as client:
                    reply = await client.chat.completions.create(**kwargs)
            if not reply.choices:
                raise ReviewUnavailable("模型没有返回审校内容，请检查模型与网关配置")
            choice = reply.choices[0]
            if choice.finish_reason == "length":
                raise ReviewUnavailable("模型输出被截断；请缩短章节，或提高本次输出上限后重试")
            if getattr(choice.message, "refusal", None):
                raise ReviewUnavailable("模型未接受本次请求，请检查输入或更换已授权的模型")
            content = choice.message.content
            if not isinstance(content, str) or not content.strip():
                raise ReviewUnavailable("模型返回了空内容；可能仅输出了推理过程或协议不兼容")
            usage = reply.usage
            return ModelReply(content, getattr(reply, "model", None) or profile.model,
                              usage.prompt_tokens if usage else None, usage.completion_tokens if usage else None)
        except ReviewUnavailable:
            raise
        except APITimeoutError as exc:
            raise ReviewUnavailable("模型请求超时，未自动重试；请检查服务或缩短章节") from exc
        except APIConnectionError as exc:
            raise ReviewUnavailable("无法连接模型接口，请检查本地网络与接口地址") from exc
        except APIStatusError as exc:
            messages = {401: "模型服务鉴权失败，请检查 API Key", 403: "模型服务拒绝访问，请检查授权",
                        429: "模型服务限流或余额不足，未自动重试"}
            raise ReviewUnavailable(messages.get(exc.status_code, f"模型服务返回 HTTP {exc.status_code}，请检查模型名与兼容参数")) from exc
        except Exception as exc:
            raise ReviewUnavailable("模型调用失败，请检查 OpenAI 兼容配置；凭证不会写入错误信息") from exc


def configured_model(control_service):
    profile = control_service.resolve_active_profile()
    configured = bool(profile and profile.api_key.strip() and profile.model.strip() and profile.protocol == "openai")
    return {"configured": configured, "model": profile.model if profile else "",
            "protocol": profile.protocol if profile else "",
            "host": urlsplit(profile.base_url).hostname if profile and profile.base_url else "",
            "message": "已配置，实际可用性以联调结果为准" if configured else "请在 AI 控制台配置并激活 OpenAI 兼容档案"}
