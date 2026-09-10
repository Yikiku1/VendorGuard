"""M1 的 Agent 循环: 单工具, 有限预算, 允许追问.

本文件只负责模型接线与循环控制; 工具行为在 agent_tools,
规则语义在 policy. client 参数接受真实 openai 客户端,
也接受测试替身, 只要提供 chat.completions.create.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
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

from .agent_tools import ToolArgumentError, check_materials, parse_tool_arguments
from .policy import PolicyDocument, StructuredFacts, load_policy

SYSTEM_PROMPT = (
    "你是供应商材料审查助手. 检查供应商材料是否满足准入要求时, "
    "必须调用 check_materials 工具, 参数从用户提交的事实原样取值, 不得改写. "
    "信息不足时可以直接追问并结束本次检查. "
    "回答时忠实转述工具结果: 规则未命中不等于准入批准, 不得自行宣布通过准入."
    "工具调用被拒绝是你的参数错误, 不是用户材料的问题, 必须改正参数重试, "
    "不得把调用失败归因于提交材料. "
    "你只能转述工具返回的处置建议, 不得声称人工审核或审批流程已被实际执行."
    "若提交的事实不足以构造合法参数, 或用户问题超出 check_materials 的能力范围, "
    "禁止编造取值(包括 null 或字符串占位), 应以问句向用户追问或说明无法回答的原因."
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


class AgentRunOutcome(BaseModel):
    """一次运行的最终产出, E 阶段的本地运行记录直接基于它."""

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
    submitted_facts: dict[str, Any] = {}


def _redact_secret(text: str) -> str:
    """把 sk- 样式的密钥串整体打码, 防止异常消息夹带 key 入档."""

    return re.sub(r"sk-[A-Za-z0-9]{8,}", "sk-***", text)


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

    def finish(kind: Literal["answer", "question", "failed"], text: str) -> AgentRunOutcome:
        return AgentRunOutcome(
            kind=kind,
            text=text,
            model_requests=model_requests,
            tool_attempts=tool_attempts,
            elapsed_seconds=time.monotonic() - started,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            tool_events=list(tool_events),
            user_request=user_request,
            policy_version=policy.version,
            submitted_facts=submitted.model_dump(),
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
            if completion.choices[0].finish_reason != "stop":
                return finish("failed", f"回答被截断或过滤: {completion.choices[0].finish_reason}")
            if time.monotonic() - started >= total_timeout:
                return finish("failed", f"响应返回时总时限 {total_timeout} 秒已用尽")
            if not text:
                return finish("failed", "模型返回空回答")
            if executed_ok:
                return finish("answer", text)
            # 无工具背书时唯一合法出口是追问(问句); 其余一律视为待纠正的结论
            if "?" in text or "\uff1f" in text:
                return finish("question", text)
            if corrections_used >= 1:
                return finish("failed", "未调用工具就给出结论, 且纠正机会已用尽")
            corrections_used += 1
            messages.append(
                ChatCompletionUserMessageParam(
                    role="user",
                    content=(
                        "必须实际调用 check_materials 工具并基于工具结果给出检查结论; "
                        "若信息不足, 请以问句形式向用户追问."
                    ),
                )
            )
            continue

        for call in calls:  # 一次返回多个调用也逐个检查预算, 非法调用同样计数
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
                        "arguments": None,
                    }
                )
                continue

            if call.function.name != "check_materials":
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
                        "detail": f"纠正机会已用尽, 再次返回未知工具: {call.function.name}",
                        "arguments": call.function.arguments,
                    }
                )
                continue

            try:
                facts = parse_tool_arguments(call.function.arguments)
                result = check_materials(facts, policy=policy, submitted=submitted)
            except ToolArgumentError as exc:
                if corrections_used >= 1:
                    tool_events.append(
                        {
                            "tool_call_id": call.id,
                            "name": call.function.name,
                            "status": "error",
                            "detail": f"纠正机会已用尽, 参数仍然非法: {exc}",
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
                    "arguments": call.function.arguments,
                }
            )

    # 循环只有 return 出口, 不会无界运行


class AgentConfigError(ValueError):
    """Agent 启动入口缺少必需配置或事实样例非法时抛出的错误."""


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


def load_submitted_facts(path: Path) -> StructuredFacts:
    """读取事实样例 JSON 并复用工具解析层校验, 错误归一为启动错误."""

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AgentConfigError(f"无法读取事实样例文件: {path}") from exc
    try:
        return parse_tool_arguments(raw_text)
    except ToolArgumentError as exc:
        raise AgentConfigError(f"事实样例文件未通过校验: {exc}") from exc


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
        "user_request": outcome.user_request,
        "policy_version": outcome.policy_version,
        "submitted_facts": outcome.submitted_facts,
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
    "load_submitted_facts",
    "main",
    "run_check",
    "write_run_log",
]


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口: python -m vendorguard.agent <事实样例文件> [请求文本].

    返回码: 0 正常回答或追问, 1 运行失败, 2 启动配置错误.
    """

    parser = argparse.ArgumentParser(prog="vendorguard.agent", description="M1 材料审查 agent")
    parser.add_argument("facts_file", type=Path, help="结构化事实样例 JSON 路径(模拟输入)")
    parser.add_argument("request", nargs="?", default="请检查这家供应商的材料是否满足准入要求.")
    args = parser.parse_args(argv)
    started_at = datetime.now()

    try:
        settings = load_llm_settings({**dotenv_values(".env"), **os.environ})
        facts = load_submitted_facts(args.facts_file)
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
    outcome = run_check(
        args.request,
        submitted=facts,
        policy=policy,
        client=client,
        model_name=settings.model_name,
    )
    label = "追问, 未经工具校验" if outcome.kind == "question" else "回答"
    print(f"[{label}] {outcome.text}")
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
