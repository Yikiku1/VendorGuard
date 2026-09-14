"""材料审查 Agent 循环: 读取材料, 核对事实与来源, 执行规则并作答.

本文件只负责模型接线与循环控制; 材料读取在 materials, 事实核对与规则执行
在 agent_tools, 规则语义在 policy. client 参数接受真实 openai 客户端,
也接受测试替身, 只要提供 chat.completions.create.

M2 的顺序规则不依赖提示词: 没读材料不能校验, 校验没过不能给结论, 缺字段
只能追问. 参考日期由程序传入, 模型只提交从材料里读到的原始事实; 会话状态
(当前材料与用户补充原文)由 agent_session 持有, 追问后的补充登记在同一会话里.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, cast

from dotenv import dotenv_values
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

from .agent_session import ReviewSession
from .agent_tools import ToolArgumentError, check_extracted_facts, parse_extracted_facts
from .materials import (
    MaterialDocument,
    MaterialReadError,
    read_text_pdf,
)
from .policy import PolicyDocument, load_policy

SYSTEM_PROMPT = (
    "你是供应商材料审查助手. 检查材料前必须先调用 read_material 读取本次材料, "
    "只能使用工具返回的原文, 不得凭记忆或猜测填写事实. "
    "如果对话里给出了用户补充的原文, 它是一份合法来源, 引用时写成 "
    "user_supplement@round:N (N 是补充轮次号), 不得把它写成材料页码; "
    "随后调用 check_materials 提交从材料里读到的事实, "
    "每项事实必须同时给出 材料ID@page:N 形式的来源页码; 材料里读不到的字段直接省略, "
    "不得编造, 也不得自行判断执照是否有效——只提交材料上写明的有效期截止日. "
    "需要向用户提问或说明无法回答时, 必须调用 ask_user 工具, 纯文本不能作为追问出口; "
    "追问中不得宣告批准, 已完成审批或检查结论. "
    "若用户问题超出材料准入检查范围(如交付率), 只能说明当前仅支持材料准入检查, "
    "不得暗示补充材料即可评估其他指标. "
    "用 ask_user 追问超范围问题时, 须先说明该指标无法评估, "
    "再询问是否需要材料检查, 不得直接索要材料. "
    "回答时忠实转述工具结果: 规则未命中不等于准入批准, 不得自行宣布通过准入."
)

READ_MATERIAL_TOOL: ChatCompletionFunctionToolParam = {
    "type": "function",
    "function": {
        "name": "read_material",
        "description": (
            "读取本次审查材料的逐页正文. 参数是本次提交材料的 ID, 只能读取这一份; "
            "核对来源时必须使用返回结果里的页码."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "material_id": {
                    "type": "string",
                    "description": "本次提交材料的 ID, 由程序给出",
                },
            },
            "required": ["material_id"],
            "additionalProperties": False,
        },
    },
}

CHECK_MATERIALS_TOOL: ChatCompletionFunctionToolParam = {
    "type": "function",
    "function": {
        "name": "check_materials",
        "description": (
            "根据从材料里读到的事实执行准入规则检查. "
            "每项事实必须同时在 sources 中给出 材料ID@page:N 格式的来源页码. "
            "无法确认的字段直接省略, 不得猜测填写; 不要提交执照是否有效的结论."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "business_license_valid_until": {
                    "type": "string",
                    "description": (
                        "材料上写明的营业执照有效期截止日, 例如 2027-08-31 或 2027年8月31日; "
                        "材料里读不到时省略"
                    ),
                },
                "category_required_documents_complete": {
                    "type": "boolean",
                    "description": "材料是否明确写明清单齐全, 读不到时省略",
                },
                "sources": {
                    "type": "object",
                    "description": "每项已读事实的来源定位, 键为字段名, 值为 材料ID@page:N",
                    "additionalProperties": {"type": "string"},
                },
            },
            "additionalProperties": False,
        },
    },
}

ASK_USER_TOOL: ChatCompletionFunctionToolParam = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "向用户提出一个补充信息的问题并结束本次运行. "
            "仅当所需信息不在材料与工具结果中时使用. "
            '参数必须是 {"question": "问题文本"} 形式的 JSON 对象. '
            "问题必须是单一问句, 不得包含批准或检查结论."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "要展示给用户的问题, 非空",
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
}


def _parse_ask_user_arguments(raw_json: str) -> str:
    """解析并校验 ask_user 参数, 非法时抛 ToolArgumentError."""

    try:
        raw = json.loads(raw_json)
    except (TypeError, ValueError) as exc:
        raise ToolArgumentError(f"追问参数不是合法 JSON: {exc}") from exc
    question = raw.get("question") if isinstance(raw, dict) else None
    if not isinstance(question, str) or not question.strip():
        raise ToolArgumentError(
            'ask_user 参数必须是 {"question": "问题文本"} 形式的 JSON 对象, '
            "question 非空且不能把问题写在其他字段或正文里"
        )
    return question


def _parse_read_material_arguments(raw_json: str) -> str:
    """解析 read_material 参数, 取出材料 ID, 非法时抛 ToolArgumentError."""

    try:
        raw = json.loads(raw_json)
    except (TypeError, ValueError) as exc:
        raise ToolArgumentError(f"读取材料参数不是合法 JSON: {exc}") from exc
    material_id = raw.get("material_id") if isinstance(raw, dict) else None
    if not isinstance(material_id, str) or not material_id.strip():
        raise ToolArgumentError(
            'read_material 参数必须是 {"material_id": "材料ID"} 形式的 JSON 对象'
        )
    return material_id


def _material_payload(material: MaterialDocument) -> dict[str, Any]:
    """按工具契约渲染材料: 材料 ID、页数与逐页文本, 页码从 1 开始."""

    return {
        "material_id": material.material_id,
        "page_count": material.page_count,
        "pages": [
            {"page": number, "text": text} for number, text in enumerate(material.pages, start=1)
        ],
    }


class AgentRunOutcome(BaseModel):
    """一次运行的最终产出, 本地运行记录直接基于它."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["answer", "question", "failed"]
    text: str
    model_requests: int
    tool_attempts: int
    elapsed_seconds: float
    prompt_tokens: int
    completion_tokens: int
    tool_events: list[dict[str, Any]]
    user_request: str = ""
    policy_version: str = ""
    material_id: str = ""
    material_sha256: str = ""
    reference_date: str = ""
    supplement_rounds: int = 0


def _redact_secret(text: str) -> str:
    """把 sk- 样式的密钥串整体打码, 防止异常消息夹带 key 入档."""

    return re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-***", text)


def run_review(
    user_request: str,
    *,
    session: ReviewSession,
    policy: PolicyDocument,
    client: OpenAI,
    reference_date: date,
    model_name: str = "qwen3.7-flash",
    max_model_requests: int = 8,
    max_tool_attempts: int = 12,
    request_timeout: float = 30.0,
    total_timeout: float = 120.0,
) -> AgentRunOutcome:
    """驱动一次有预算的材料审查循环, 返回检查说明或明确失败.

    状态推进 (M2 方案第 6 节): read_material 成功才算读过材料; 校验成功且
    没有缺失事实才允许输出检查说明; 有缺失事实时只允许 ask_user 追问.
    检查说明返回 kind=answer, ask_user 返回 kind=question; 预算耗尽、超时、
    网络失败和纠正用尽返回 kind=failed.
    """

    started = time.monotonic()
    material = session.material
    sources = session.sources()
    opening_parts = [f"请审查这份材料: {material.source_name} (材料 ID: {material.material_id})."]
    for round_number, supplement_text in enumerate(session.supplements, start=1):
        opening_parts.append(
            f"用户在第 {round_number} 轮追问后补充的原文 "
            f"(来源定位 user_supplement@round:{round_number}): {supplement_text}"
        )
    opening_parts.append(user_request)
    messages: list[ChatCompletionMessageParam] = [
        ChatCompletionSystemMessageParam(role="system", content=SYSTEM_PROMPT),
        ChatCompletionUserMessageParam(role="user", content=" ".join(opening_parts)),
    ]
    read_done = False
    checked_ok = False
    needs_followup = False
    model_requests = 0
    tool_attempts = 0
    corrections_used = 0
    ask_correction_used = False
    prompt_tokens = 0
    completion_tokens = 0
    tool_events: list[dict[str, Any]] = []

    def finish(kind: Literal["answer", "question", "failed"], text: str) -> AgentRunOutcome:
        return AgentRunOutcome(
            kind=kind,
            text=_redact_secret(text),
            model_requests=model_requests,
            tool_attempts=tool_attempts,
            elapsed_seconds=time.monotonic() - started,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            tool_events=list(tool_events),
            user_request=user_request,
            policy_version=policy.version,
            material_id=material.material_id,
            material_sha256=material.sha256,
            reference_date=reference_date.isoformat(),
            supplement_rounds=session.supplement_rounds,
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
                tools=[READ_MATERIAL_TOOL, CHECK_MATERIALS_TOOL, ASK_USER_TOOL],
                timeout=min(request_timeout, max(remaining, 0.001)),
                extra_body={"enable_thinking": False},
            )
        except Exception as exc:
            return finish("failed", f"模型请求失败: {exc}")

        if completion.usage is not None:
            prompt_tokens += completion.usage.prompt_tokens
            completion_tokens += completion.usage.completion_tokens

        if not completion.choices:
            return finish("failed", "模型响应缺少 choices, 可能被内容过滤拦截")

        message = completion.choices[0].message
        # assistant 的 tool_calls 必须原样回写历史, 调用 ID 对应是协议要求;
        # SDK 的 dump 结果与 assistant 消息协议一致, cast 仅向类型系统声明这一点.
        messages.append(
            cast(
                ChatCompletionAssistantMessageParam,
                message.model_dump(exclude_none=True),
            )
        )
        calls = message.tool_calls or []

        # 评审修复 #4: 文本与工具两条路径共用同一道迟到检查
        if time.monotonic() - started >= total_timeout:
            return finish("failed", f"响应返回时总时限 {total_timeout} 秒已用尽")

        if not calls:
            text = (message.content or "").strip()
            if completion.choices[0].finish_reason != "stop":
                return finish("failed", f"回答被截断或过滤: {completion.choices[0].finish_reason}")
            if not text:
                return finish("failed", "模型返回空回答")
            if checked_ok:
                return finish("answer", text)
            if needs_followup:
                correction_detail = "校验结果仍有缺失事实, 追问必须调用 ask_user"
                exhausted_text = "校验结果仍有缺失事实, 且模型未调用 ask_user"
                correction_message = (
                    "校验结果包含 missing_fields; 必须调用 ask_user 提出一个补充信息问题."
                )
            else:
                correction_detail = "纯文本不能作为结论或追问出口"
                exhausted_text = "未调用工具就给出结论, 且纠正机会已用尽"
                correction_message = (
                    "检查结论必须来自 check_materials 工具结果, 且校验结果不能有缺失事实; "
                    "若需要向用户提问, 请调用 ask_user 工具."
                )
            if corrections_used >= 1:
                tool_events.append(
                    {
                        "tool_call_id": "<text>",
                        "name": "<text-correction>",
                        "status": "error",
                        "detail": f"纠正机会已用尽: {correction_detail}",
                        "assistant_content": text[:200],
                        "arguments": None,
                    }
                )
                return finish("failed", exhausted_text)
            corrections_used += 1
            tool_events.append(
                {
                    "tool_call_id": "<text>",
                    "name": "<text-correction>",
                    "status": "error",
                    "detail": correction_detail,
                    "assistant_content": text[:200],
                    "arguments": None,
                }
            )
            messages.append(
                ChatCompletionUserMessageParam(
                    role="user",
                    content=correction_message,
                )
            )
            continue

        if len(calls) > 1:
            # 评审修复: 一次响应限定一个工具调用, 避免"追问与执行并发"的含混行为
            tool_attempts += len(calls)
            if tool_attempts >= max_tool_attempts:
                return finish("failed", f"工具调用尝试达到上限 {max_tool_attempts} 次")
            for bad_call in calls:
                detail = "一次响应只允许一个工具调用"
                tool_events.append(
                    {
                        "tool_call_id": bad_call.id,
                        "name": (
                            bad_call.function.name
                            if bad_call.type == "function"
                            else f"<type:{bad_call.type}>"
                        ),
                        "status": "error",
                        "detail": detail,
                        "arguments": (
                            bad_call.function.arguments if bad_call.type == "function" else None
                        ),
                    }
                )
                messages.append(
                    ChatCompletionToolMessageParam(
                        role="tool",
                        tool_call_id=bad_call.id,
                        content=json.dumps({"error": detail}, ensure_ascii=False),
                    )
                )
            if corrections_used >= 1:
                return finish("failed", "纠正机会已用尽, 模型仍返回多个工具调用")
            corrections_used += 1
            continue

        for call in calls:  # 一次返回多个调用也逐个检查预算, 非法调用同样计数
            if tool_attempts >= max_tool_attempts:
                return finish("failed", f"工具调用尝试达到上限 {max_tool_attempts} 次")
            tool_attempts += 1

            if call.type != "function":
                # 非 function 类型的调用一律按未知工具处理, 不执行
                if corrections_used >= 1:
                    return finish(
                        "failed",
                        f"纠正机会已用尽, 再次返回不支持的调用类型: {call.type}",
                    )
                corrections_used += 1
                messages.append(
                    ChatCompletionToolMessageParam(
                        role="tool",
                        tool_call_id=call.id,
                        content=json.dumps(
                            {"error": f"不支持的工具调用类型: {call.type}"},
                            ensure_ascii=False,
                        ),
                    )
                )
                tool_events.append(
                    {
                        "tool_call_id": call.id,
                        "name": f"<type:{call.type}>",
                        "status": "error",
                        "detail": f"不支持的工具调用类型: {call.type}",
                        "arguments": None,
                    }
                )
                continue

            if call.function.name not in (
                "read_material",
                "check_materials",
                "ask_user",
            ):
                if corrections_used >= 1:
                    tool_events.append(
                        {
                            "tool_call_id": call.id,
                            "name": call.function.name,
                            "status": "error",
                            "detail": f"纠正机会已用尽, 再次返回未知工具: {call.function.name}",
                            "arguments": call.function.arguments,
                        }
                    )
                    return finish(
                        "failed",
                        f"纠正机会已用尽, 再次返回未知工具: {call.function.name}",
                    )
                corrections_used += 1
                messages.append(
                    ChatCompletionToolMessageParam(
                        role="tool",
                        tool_call_id=call.id,
                        content=json.dumps(
                            {"error": f"未知工具: {call.function.name}"},
                            ensure_ascii=False,
                        ),
                    )
                )
                tool_events.append(
                    {
                        "tool_call_id": call.id,
                        "name": call.function.name,
                        "status": "error",
                        "detail": f"纠正机会已用尽, 再次返回未知工具: {call.function.name}",
                        "arguments": call.function.arguments,
                    }
                )
                continue

            if call.function.name == "ask_user":
                try:
                    question = _parse_ask_user_arguments(call.function.arguments)
                except ToolArgumentError as exc:
                    if ask_correction_used:
                        tool_events.append(
                            {
                                "tool_call_id": call.id,
                                "name": "ask_user",
                                "status": "error",
                                "detail": f"追问参数非法, 专属纠正机会已用尽: {exc}",
                                "assistant_content": (message.content or "")[:200],
                                "arguments": call.function.arguments,
                            }
                        )
                        return finish("failed", f"追问参数非法, 专属纠正机会已用尽: {exc}")
                    ask_correction_used = True
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
                            "name": "ask_user",
                            "status": "error",
                            "detail": str(exc),
                            "assistant_content": (message.content or "")[:200],
                            "arguments": call.function.arguments,
                        }
                    )
                    continue
                tool_events.append(
                    {
                        "tool_call_id": call.id,
                        "name": "ask_user",
                        "status": "ok",
                        "detail": {"question": question},
                        "arguments": call.function.arguments,
                    }
                )
                return finish("question", question)

            if call.function.name == "read_material":
                try:
                    requested_id = _parse_read_material_arguments(call.function.arguments)
                except ToolArgumentError as exc:
                    if corrections_used >= 1:
                        tool_events.append(
                            {
                                "tool_call_id": call.id,
                                "name": "read_material",
                                "status": "error",
                                "detail": f"纠正机会已用尽, 读取参数仍然非法: {exc}",
                                "assistant_content": (message.content or "")[:200],
                                "arguments": call.function.arguments,
                            }
                        )
                        return finish("failed", f"纠正机会已用尽, 读取参数仍然非法: {exc}")
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
                            "name": "read_material",
                            "status": "error",
                            "detail": str(exc),
                            "assistant_content": (message.content or "")[:200],
                            "arguments": call.function.arguments,
                        }
                    )
                    continue

                if requested_id != material.material_id:
                    # 材料白名单: 只认本次会话登记的那一份, 模型不能读别的文件
                    detail = (
                        f"材料 {requested_id} 不属于本次审查, 本次只能读取 {material.material_id}"
                    )
                    if corrections_used >= 1:
                        tool_events.append(
                            {
                                "tool_call_id": call.id,
                                "name": "read_material",
                                "status": "error",
                                "detail": f"纠正机会已用尽, 材料 ID 仍然非法: {detail}",
                                "arguments": call.function.arguments,
                            }
                        )
                        return finish("failed", f"纠正机会已用尽, 材料 ID 仍然非法: {detail}")
                    corrections_used += 1
                    messages.append(
                        ChatCompletionToolMessageParam(
                            role="tool",
                            tool_call_id=call.id,
                            content=json.dumps({"error": detail}, ensure_ascii=False),
                        )
                    )
                    tool_events.append(
                        {
                            "tool_call_id": call.id,
                            "name": "read_material",
                            "status": "error",
                            "detail": detail,
                            "arguments": call.function.arguments,
                        }
                    )
                    continue

                read_done = True
                tool_events.append(
                    {
                        "tool_call_id": call.id,
                        "name": "read_material",
                        "status": "ok",
                        "detail": {
                            "material_id": material.material_id,
                            "page_count": material.page_count,
                        },
                        "arguments": call.function.arguments,
                    }
                )
                messages.append(
                    ChatCompletionToolMessageParam(
                        role="tool",
                        tool_call_id=call.id,
                        content=json.dumps(_material_payload(material), ensure_ascii=False),
                    )
                )
                continue

            try:
                facts = parse_extracted_facts(call.function.arguments)
            except ToolArgumentError as exc:
                if corrections_used >= 1:
                    tool_events.append(
                        {
                            "tool_call_id": call.id,
                            "name": call.function.name,
                            "status": "error",
                            "detail": f"纠正机会已用尽, 参数仍然非法: {exc}",
                            "assistant_content": (message.content or "")[:200],
                            "arguments": call.function.arguments,
                        }
                    )
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
                        "arguments": call.function.arguments,
                    }
                )
                continue

            if not read_done:
                # 顺序规则: 事实必须来自材料原文, 所以校验之前必须读过材料
                detail = "尚未读取本次材料, 必须先调用 read_material 再提交事实"
                if corrections_used >= 1:
                    tool_events.append(
                        {
                            "tool_call_id": call.id,
                            "name": "check_materials",
                            "status": "error",
                            "detail": f"纠正机会已用尽, 校验之前仍未读取材料: {detail}",
                            "assistant_content": (message.content or "")[:200],
                            "arguments": call.function.arguments,
                        }
                    )
                    return finish("failed", f"纠正机会已用尽, 校验之前仍未读取材料: {detail}")
                corrections_used += 1
                messages.append(
                    ChatCompletionToolMessageParam(
                        role="tool",
                        tool_call_id=call.id,
                        content=json.dumps({"error": detail}, ensure_ascii=False),
                    )
                )
                tool_events.append(
                    {
                        "tool_call_id": call.id,
                        "name": "check_materials",
                        "status": "error",
                        "detail": detail,
                        "arguments": call.function.arguments,
                    }
                )
                continue

            try:
                result = check_extracted_facts(
                    facts,
                    sources=sources,
                    policy=policy,
                    reference_date=reference_date,
                )
            except ToolArgumentError as exc:
                if corrections_used >= 1:
                    tool_events.append(
                        {
                            "tool_call_id": call.id,
                            "name": call.function.name,
                            "status": "error",
                            "detail": f"纠正机会已用尽, 来源核对仍然失败: {exc}",
                            "assistant_content": (message.content or "")[:200],
                            "arguments": call.function.arguments,
                        }
                    )
                    return finish("failed", f"纠正机会已用尽, 来源核对仍然失败: {exc}")
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
                        "arguments": call.function.arguments,
                    }
                )
                continue

            except Exception as exc:
                tool_events.append(
                    {
                        "tool_call_id": call.id,
                        "name": "check_materials",
                        "status": "error",
                        "detail": f"工具内部错误: {exc}",
                        "arguments": call.function.arguments,
                    }
                )
                return finish("failed", f"工具内部错误: {exc}")

            checked_ok = not result.missing_fields
            needs_followup = bool(result.missing_fields)
            messages.append(
                ChatCompletionToolMessageParam(
                    role="tool",
                    tool_call_id=call.id,
                    content=result.model_dump_json(),
                )
            )
            messages.append(
                ChatCompletionUserMessageParam(
                    role="user",
                    content=(
                        "本次工具仅执行只读规则检查, 没有写入案件状态. "
                        "outcome 中的 action 与 target_case_status 只是建议动作; "
                        "只能描述为建议, 不得声称系统已经执行、标记或改变案件状态."
                    ),
                )
            )
            tool_events.append(
                {
                    "tool_call_id": call.id,
                    "name": "check_materials",
                    "status": "ok",
                    "detail": result.model_dump(),
                    "arguments": call.function.arguments,
                }
            )

    # 循环只有 return 出口, 不会无界运行


class AgentConfigError(ValueError):
    """Agent 启动入口缺少必需配置或材料无法读取时抛出的错误."""


class LlmSettings(BaseModel):
    """模型接线必需的三项配置, 只在 Agent 启动入口检查."""

    model_config = ConfigDict(extra="forbid")

    model_name: str
    api_key: str
    base_url: str


_REQUIRED_ENV = {
    "VENDORGUARD_LLM_MODEL": "model_name",
    "VENDORGUARD_LLM_API_KEY": "api_key",
    "VENDORGUARD_LLM_BASE_URL": "base_url",
}


def load_llm_settings(env: Mapping[str, str | None]) -> LlmSettings:
    """从环境变量映射构造模型配置, 缺失时一次报全所有名字.

    dotenv 读出的值可能为 None, 与未定义同样按缺失处理;
    str() 只是向类型系统声明已通过缺失检查.
    """

    missing = [name for name in _REQUIRED_ENV if not env.get(name)]
    if missing:
        raise AgentConfigError("缺少模型配置: " + ", ".join(missing) + ", 请写入本地 .env")
    return LlmSettings(**{field: str(env[name]) for name, field in _REQUIRED_ENV.items()})


def load_material(path: Path) -> MaterialDocument:
    """读取命令行给出的材料 PDF, 失败归一为启动错误.

    材料层已经区分文件不存在、不是 PDF、扫描件和空白材料, 这里只把失败码
    翻译成启动期错误消息, 不改变判定, 也不在启动阶段做模型请求.
    """

    try:
        return read_text_pdf(path)
    except MaterialReadError as exc:
        raise AgentConfigError(f"材料无法作为文本 PDF 处理: {exc}") from exc


def write_run_log(
    outcome: AgentRunOutcome,
    *,
    model_name: str,
    log_dir: Path,
    started_at: datetime,
    simulated: bool = True,
) -> Path:
    """把一次运行落盘为脱敏 JSON, 返回写入的文件路径.

    记录模型标识, 材料身份 (ID 与哈希), 参考日期, 工具事件, 最终文本,
    耗时与 token 用量; 不写 API Key 或请求头, 对 sk- 样式密钥做兜底打码.
    started_at 由调用方显式传入, 保证文件名可测且同一秒不覆盖.
    """

    log_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "started_at": started_at.isoformat(timespec="seconds"),
        "model": model_name,
        "user_request": outcome.user_request,
        "policy_version": outcome.policy_version,
        "material_id": outcome.material_id,
        "material_sha256": outcome.material_sha256,
        "reference_date": outcome.reference_date,
        "supplement_rounds": outcome.supplement_rounds,
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


__all__ = [
    "AgentConfigError",
    "AgentRunOutcome",
    "load_llm_settings",
    "load_material",
    "main",
    "run_review",
    "write_run_log",
]


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口: python -m vendorguard.agent <材料PDF> [请求文本].

    --reference-date 决定执照状态怎么派生, 缺省用今天; 演示固定样例时可以
    显式传 2026-09-01 与案例 YAML 的 reference_date 对齐。
    返回码: 0 正常回答或追问, 1 运行失败, 2 启动配置错误。
    """

    parser = argparse.ArgumentParser(prog="vendorguard.agent", description="材料审查 agent")
    parser.add_argument("material_file", type=Path, help="待审查的文本 PDF 材料路径")
    parser.add_argument("request", nargs="?", default="请检查这家供应商的材料是否满足准入要求.")
    parser.add_argument(
        "--reference-date",
        type=date.fromisoformat,
        default=None,
        help="派生执照状态用的参考日期 (YYYY-MM-DD), 缺省用今天",
    )
    args = parser.parse_args(argv)
    started_at = datetime.now()
    reference_date = args.reference_date or date.today()

    try:
        settings = load_llm_settings({**dotenv_values(".env"), **os.environ})
        material = load_material(args.material_file)
        policy = load_policy(Path("policies/rules/v1.0.0.yaml"))
    except AgentConfigError as exc:
        print(f"启动失败: {exc}")
        return 2

    client = OpenAI(
        api_key=settings.api_key,
        base_url=settings.base_url,
        timeout=30.0,
        max_retries=0,
    )
    session = ReviewSession.start(material)
    outcome = run_review(
        args.request,
        session=session,
        policy=policy,
        client=client,
        model_name=settings.model_name,
        reference_date=reference_date,
    )
    labels = {"answer": "检查说明", "question": "追问", "failed": "运行失败"}
    print(f"[{labels[outcome.kind]}] {outcome.text}")
    if outcome.kind == "question":
        # M2 只接受一次补充: 回答登记为来源后, 在同一会话里重新读取与校验
        answer = input("请补充(直接回车放弃): ").strip()
        if answer:
            round_number = session.record_supplement(answer)
            print(f"[已登记第 {round_number} 轮用户补充]")
            outcome = run_review(
                args.request,
                session=session,
                policy=policy,
                client=client,
                model_name=settings.model_name,
                reference_date=reference_date,
            )
            print(f"[{labels[outcome.kind]}] {outcome.text}")
    for event in outcome.tool_events:
        if event["name"] == "check_materials" and event["status"] == "ok":
            raw = json.dumps(event["detail"], ensure_ascii=False)
            print(f"原始校验结果: {raw}")
    log_path = write_run_log(
        outcome,
        model_name=settings.model_name,
        log_dir=Path("logs/agent-runs"),
        started_at=started_at,
    )
    print(f"运行记录: {log_path}")
    return 0 if outcome.kind in ("answer", "question") else 1


if __name__ == "__main__":
    raise SystemExit(main())
