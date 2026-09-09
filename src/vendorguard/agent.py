"""M1 的 Agent 循环: 单工具, 有限预算, 允许追问.

本文件只负责模型接线与循环控制; 工具行为在 agent_tools,
规则语义在 policy. client 参数接受真实 openai 客户端,
也接受测试替身, 只要提供 chat.completions.create.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from openai import OpenAI
from openai.types.chat import (
    ChatCompletionAssistantMessageParam,
    ChatCompletionFunctionToolParam,
    ChatCompletionMessageParam,
    ChatCompletionSystemMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionUserMessageParam,
)
from pydantic import BaseModel, ConfigDict

from .agent_tools import ToolArgumentError, check_materials, parse_tool_arguments
from .policy import PolicyDocument, StructuredFacts

SYSTEM_PROMPT = (
    "你是供应商材料审查助手. 检查供应商材料是否满足准入要求时, "
    "必须调用 check_materials 工具, 参数从用户提交的事实原样取值, 不得改写. "
    "信息不足时可以直接追问并结束本次检查. "
    "回答时忠实转述工具结果: 规则未命中不等于准入批准, 不得自行宣布通过准入."
)

CHECK_MATERIALS_TOOL: ChatCompletionFunctionToolParam = {
    "type": "function",
    "function": {
        "name": "check_materials",
        "description": (
            "根据用户提交的材料事实执行准入规则检查. "
            "每项已知事实必须同时在 sources 中给出 文档ID@定位 格式的来源. "
            "无法确认的字段直接省略, 不得猜测填写."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "business_license_document_status": {
                    "type": "string",
                    "enum": ["valid", "expired", "inconsistent", "unreadable"],
                    "description": "营业执照材料状态, 未知时省略",
                },
                "category_required_documents_complete": {
                    "type": "boolean",
                    "description": "品类必填材料是否齐全, 未知时省略",
                },
                "sources": {
                    "type": "object",
                    "description": "每个人已知事实的来源定位, 键为字段名",
                    "additionalProperties": {"type": "string"},
                },
            },
            "additionalProperties": False,
        },
    },
}

# 启发式: 未执行过工具却说出这些词, 视为"声称已检查". 刻意保守, 只拦明显越界.
_CHECK_CLAIM_TOKENS = ("not_hit", "未命中", "已检查", "检查完成", "工具结果")


class AgentRunOutcome(BaseModel):
    """一次运行的最终产出, E 阶段的本地运行记录直接基于它. """
    
    model_config = ConfigDict(extra="forbid")
    
    kind: Literal["answer", "failed"]
    text: str
    model_requests: int
    tool_attempts: int
    elapsed_seconds: float
    prompt_tokens: int
    completion_tokens: int
    tool_events: list[dict[str, Any]]


def _redact_secret(text: str) -> str:
    """把 sk- 样式的密钥串整体打码, 防止异常消息夹带 key 入档."""
    
    return re.sub(r"sk-[A-Za-z0-9]{8,}", "sk-***", text)


def _claims_checked(text: str) -> bool:
    """判断一段无工具调用的回答是否在声称已执行检查."""
    
    return any(token in text for token in _CHECK_CLAIM_TOKENS)


def run_check(
    user_request: str,
    *,
    submitted: StructuredFacts,
    policy: PolicyDocument,
    client: OpenAI,
    model_name: str = "qwen3.7-flash",
    max_model_requests: int = 8,
    max_tool_attempts: int = 12,
    request_timeout: float = 30.0,
    total_timeout: float = 120.0,
) -> AgentRunOutcome:
    """驱动一次有预算的工具调用循环, 返回回答或明确失败.

    追问与检查说明都返回 kind=answer; 预算耗尽, 超时, 网络失败和
    纠正用尽返回 kind=failed, 不吞错继续跑.
    """
    
    started = time.monotonic()
    facts_json = json.dumps(submitted.model_dump(), ensure_ascii=False)
    messages: list[ChatCompletionMessageParam] = [
        ChatCompletionSystemMessageParam(role="system", content=SYSTEM_PROMPT),
        ChatCompletionUserMessageParam(
            role="user", content=f"本次提交的事实(模拟输入): {facts_json}. {user_request}"
        ),
    ]
    model_requests = 0
    tool_attempts = 0
    corrections_used = 0
    executed_ok = False
    prompt_tokens = 0
    completion_tokens = 0
    tool_events: list[dict[str, Any]] = []
    
    def finish(kind: Literal["answer", "failed"], text: str) -> AgentRunOutcome:
        return AgentRunOutcome(
            kind=kind,
            text=text,
            model_requests=model_requests,
            tool_attempts=tool_attempts,
            elapsed_seconds=time.monotonic() - started,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            tool_events=list(tool_events),
        )

    while True:
        remaining = total_timeout - (time.monotonic() - started)
        if remaining <= 0:
            return finish("failed", f"总时限 {total_timeout} 秒已用尽")
        if model_requests >= max_model_requests:
            return finish("failed", f"模型请求达到上限 {max_model_requests} 次")
        model_requests += 1

        try:
            completion = client.chat.completions.create(
                model=model_name,
                messages=messages,
                tools=[CHECK_MATERIALS_TOOL],
                timeout=min(request_timeout, max(remaining, 0.001)),
                extra_body={"enable_thinking": False},
            )
        except Exception as exc:
            return finish("failed", f"模型请求失败: {exc}")
        
        if completion.usage is not None:
            prompt_tokens += completion.usage.prompt_tokens
            completion_tokens += completion.usage.completion_tokens
        
        message = completion.choices[0].message
        # assistant 的 tool_calls 必须原样回写历史, 调用 ID 对应是协议要求;
        # SDK 的 dump 结果与 assistant 消息协议一致, cast 仅向类型系统声明这一点.
        messages.append(
            cast(ChatCompletionAssistantMessageParam, message.model_dump(exclude_none=True))
        )
        calls = message.tool_calls or []
        
        if not calls:
            text = (message.content or "").strip()
            if not executed_ok and _claims_checked(text):
                if corrections_used >= 1:
                    return finish("failed", "未成功校验就声称已检查, 且纠正机会已用尽")
                corrections_used += 1
                messages.append(
                    ChatCompletionUserMessageParam(
                        role="user",
                        content=(
                            "请实际调用 check_materials 工具, 根据工具结果回答, "
                            "不要凭推断下结论."
                        ),
                    )
                )
                continue
            return finish("answer", text) # 追问或检查说明都走这里
        
        for call in calls:  #一次返回多个调用也逐个检查预算, 非法调用同样计数
            if tool_attempts >= max_tool_attempts:
                return finish("failed", f"工具调用尝试达到上限 {max_tool_attempts} 次")
            tool_attempts += 1

            if call.type != "function":
                # 非 function 类型的调用一律按未知工具处理, 不执行
                if corrections_used >= 1:
                    return finish(
                        "failed", f"纠正机会已用尽, 再次返回不支持的调用类型: {call.type}"
                    )
                corrections_used += 1
                messages.append(
                    ChatCompletionToolMessageParam(
                        role="tool",
                        tool_call_id=call.id,
                        content=json.dumps(
                            {"error": f"不支持的工具调用类型: {call.type}"}, ensure_ascii=False
                        ),
                    )
                )
                tool_events.append(
                    {
                        "tool_call_id": call.id,
                        "name": f"<type:{call.type}>",
                        "status": "error",
                        "detail": f"不支持的工具调用类型: {call.type}",
                    }
                )
                continue

            if call.function.name != "check_materials":
                if corrections_used >= 1:
                    return finish(
                        "failed", f"纠正机会已用尽, 再次返回未知工具: {call.function.name}"
                    )
                corrections_used += 1
                messages.append(
                    ChatCompletionToolMessageParam(
                        role="tool",
                        tool_call_id=call.id,
                        content=json.dumps(
                            {"error": f"未知工具: {call.function.name}"}, ensure_ascii=False
                        ),
                    )
                )
                tool_events.append(
                    {
                        "tool_call_id": call.id,
                        "name": call.function.name,
                        "status": "error",
                        "detail": f"未知工具: {call.function.name}",
                    }
                )
                continue
            
            try:
                facts = parse_tool_arguments(call.function.arguments)
                result = check_materials(facts, policy=policy, submitted=submitted)
            except ToolArgumentError as exc:
                if corrections_used >= 1:
                    return finish("failed", f"纠正机会已用尽, 参数仍然非法: {exc}")
                corrections_used += 1
                messages.append(
                    ChatCompletionToolMessageParam(
                        role="tool",
                        tool_call_id=call.id,
                        content=json.dumps({"error": str(exc)}, ensure_ascii=False),
                    )
                )
                tool_events.append(
                    {
                        "tool_call_id": call.id,
                        "name": call.function.name,
                        "status": "error",
                        "detail": str(exc),
                    }
                )
                continue

            executed_ok = True
            messages.append(
                ChatCompletionToolMessageParam(
                    role="tool",
                    tool_call_id=call.id,
                    content=result.model_dump_json(),
                )
            )
            tool_events.append(
                {
                    "tool_call_id": call.id,
                    "name": "check_materials",
                    "status": "ok",
                    "detail": result.model_dump(),
                }
            )

    # 循环只有 return 出口, 不会无界运行

def write_run_log(
    outcome: AgentRunOutcome,
    *,
    model_name: str,
    log_dir: Path,
    started_at: datetime,
    simulated: bool = True,
) -> Path:
    """把一次运行落盘为脱敏 JSON, 返回写入的文件路径.

    记录模型标识, 模拟输入标记, 工具事件, 最终文本, 耗时与 token
    用量; 不写 API Key 或请求头, 对 sk- 样式密钥做兜底打码.
    started_at 由调用方显式传入, 保证文件名可测且同一秒不覆盖.
    """
    
    log_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "started_at": started_at.isoformat(timespec="seconds"),
        "model": model_name,
        "simulated_input": simulated,
        "kind": outcome.kind,
        "text": outcome.text,
        "model_requests": outcome.model_requests,
        "tool_attempts": outcome.tool_attempts,
        "elapsed_seconds": outcome.elapsed_seconds,
        "usage": {
            "prompt_tokens": outcome.prompt_tokens,
            "completion_tokens": outcome.completion_tokens,
        },
        "tool_events": outcome.tool_events,
    }
    path = log_dir / f"{started_at:%Y%m%dT%H%M%S%f}.json"
    raw_json = json.dumps(payload, ensure_ascii=False, indent=2)
    path.write_text(_redact_secret(raw_json), encoding="utf-8")
    return path

__all__ = ["AgentRunOutcome", "run_check", "write_run_log"]