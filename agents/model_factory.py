from __future__ import annotations

import json
import os
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI

from .models import AgentResult


def create_chat_model(config: Any):
    provider = str(getattr(config, "agent_provider", "deepseek")).lower()
    timeout = float(getattr(config, "agent_timeout_seconds", 20.0))
    model_name = str(getattr(config, "agent_model", "deepseek-chat"))
    base_url = str(getattr(config, "agent_api_url", "")).rstrip("/") or None
    if provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            return None
        return ChatOpenAI(
            model=model_name,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=1,
            temperature=0.1,
        )
    if provider != "deepseek":
        raise ValueError(f"unsupported agent provider: {provider}")
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return None
    return ChatDeepSeek(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_retries=1,
        temperature=0.1,
    )


class AgentModelClient:
    def __init__(self, config: Any) -> None:
        self.config = config

    def model(self):
        return create_chat_model(self.config)

    def enhance(self, result: AgentResult) -> AgentResult:
        model = self.model()
        if model is None:
            provider = str(getattr(self.config, "agent_provider", "deepseek")).upper()
            return result.with_enhancement_error(
                f"未设置 {provider}_API_KEY，已使用本地规则结果。"
            )
        try:
            response = model.invoke(
                [
                    SystemMessage(
                        content=(
                            "你是局域网文件同步系统诊断助手。只能依据脱敏 JSON。"
                            "只输出 JSON 对象，字段为 summary、causes、recommendations；"
                            "causes 和 recommendations 是中文字符串数组。"
                        )
                    ),
                    HumanMessage(
                        content=json.dumps(
                            result.safe_payload(),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    ),
                ]
            )
            content = response.content
            if isinstance(content, list):
                content = "".join(
                    str(item.get("text", "")) if isinstance(item, dict) else str(item)
                    for item in content
                )
            raw = str(content).strip()
            if raw.startswith("```"):
                raw = raw.strip("`")
                if raw.startswith("json"):
                    raw = raw[4:].lstrip()
            payload = json.loads(raw)
            summary = str(payload.get("summary", "")).strip()
            causes = _strings(payload.get("causes"))
            recommendations = _strings(payload.get("recommendations"))
            if not summary and not causes and not recommendations:
                raise ValueError("model returned an empty analysis")
            return result.with_enhancement(
                summary=summary,
                causes=causes,
                recommendations=recommendations,
            )
        except Exception as exc:
            return result.with_enhancement_error(
                f"模型增强失败（{type(exc).__name__}），已使用本地规则结果。"
            )


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
