"""M2+M3 完整链路: 真实材料读数, 规则校验, 追问补充, 检索与带来源报告.

与 M1 的差别:

```text
M1: 程序持有真值 -> 模型只回显 -> 快照对账挡住改写
M2: 模型从材料读数 -> 程序核对"值是否真的出现在所声明的来源里" -> 挡住编造
M3: 模型只能引用检索实际返回的片段 -> 程序核对引用标签 -> 挡住伪造出处
```

三条边界都是结构性的, 不依赖提示词自觉:

- `check_materials` 走 `material_facts.check_extracted_facts`, 核对失败时任何
  规则都不执行。
- 结论只能经 `submit_report` 工具给出, 纯文本不是出口。
- `submit_report` 的引用标签必须来自本次 `search_policy` 的真实返回, 编造
  出处会被拒绝并要求纠正; 叙述里宣告准入结论同样被机械拦截。

本模块不访问数据库, 不写案件状态, 不跑迁移; 产出只是**初审建议**, 不是
供应商准入审批。结构上分为三块: 工具参数解析与文字检查(纯函数)、
`_ReviewLoop`(单轮循环与各工具处理)、命令行入口。
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime
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

from .agent import _redact_secret
from .agent_session import ReviewSession, SessionError
from .material_facts import (
    ExtractedFacts,
    ExtractionError,
    MaterialCheckResult,
    check_extracted_facts,
)
from .policy import PolicyDocument
from .retrieval import KnowledgeIndex, RetrievalError

DEFAULT_REFERENCE_DATE = date(2026, 9, 1)

SYSTEM_PROMPT = (
    "你是供应商材料初审助手。请按以下顺序工作, 每一步都必须通过工具完成, 不得凭记忆或推测: "
    "1) 调用 read_material 读取用户提交的材料原文; "
    "2) 从材料原文里读数, 调用 check_materials 提交你读到的事实, 并为每个事实给出 sources, "
    "格式为 材料ID@page:页码(用户补充的内容写作 用户补充@R轮次); "
    "材料里若有“清单状态: 已齐全”这类明确语句, 据此提交 category_required_documents_complete; "
    "事实在材料里读不到时直接省略该字段, 禁止猜测或用 null 填充。"
    "3) 若校验结果含 missing_fields, 必须调用 ask_user 提出一个具体的补充问题, 不要自行假设。"
    "4) 调用 search_policy 检索制度依据, 用检索返回的原文说明判断依据。"
    "5) 校验通过且无缺失字段后, 调用 submit_report 给出初审建议。"
    "结论只能经 submit_report 给出, 纯文本不算结论。"
    "引用出处时只能使用 search_policy 实际返回的 edition_key 与 locator, 不得编造。"
    "报告叙述中不得宣告准入结论(如“准入通过”“建议批准”), 只能描述检查结果与建议动作。"
    "体检结果里规则未命中不等于准入批准; 你只能给出建议, 不得声称系统已批准或已改变案件状态。"
)

READ_MATERIAL_TOOL: ChatCompletionFunctionToolParam = {
    "type": "function",
    "function": {
        "name": "read_material",
        "description": "读取本次提交的材料原文, 按页码返回可核对的文本。",
        "parameters": {
            "type": "object",
            "properties": {
                "material_id": {"type": "string", "description": "材料 ID, 由用户在本次提交中给出"},
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
            "校验从材料读出的事实是否满足准入规则。"
            "每个已知事实必须在 sources 中给出真实来源, 值必须能在该来源原文里找到; "
            "读不到的字段直接省略, 不要猜测。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "business_license_declared_valid_until": {
                    "type": "string",
                    "description": "营业执照声明的有效期截止日, 格式 YYYY-MM-DD; 读不到则省略",
                },
                "category_required_documents_complete": {
                    "type": "boolean",
                    "description": (
                        "品类必填材料是否齐全; 材料里有“清单状态”之类的明确语句时据此填写, "
                        "无法判断则省略"
                    ),
                },
                "sources": {
                    "type": "object",
                    "description": (
                        "每个已提交事实的来源定位, 键为字段名。"
                        "凡提交了某个事实, 就必须为它给出 sources 项; 缺来源会被拒绝。"
                        "读不到的字段请整个省略, 不要提交空字符串或 null。"
                    ),
                    "additionalProperties": {"type": "string"},
                },
            },
            "additionalProperties": False,
        },
    },
}

SEARCH_POLICY_TOOL: ChatCompletionFunctionToolParam = {
    "type": "function",
    "function": {
        "name": "search_policy",
        "description": "按参考日期检索现行有效制度原文, 返回可引用的版本、定位符与原文片段。",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "检索问题或关键词"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}

ASK_USER_TOOL: ChatCompletionFunctionToolParam = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": "材料里读不到必需信息时, 向用户提出一个具体的补充问题。",
        "parameters": {
            "type": "object",
            "properties": {"prompt": {"type": "string", "description": "要问用户的问题"}},
            "required": ["prompt"],
            "additionalProperties": False,
        },
    },
}

SUBMIT_REPORT_TOOL: ChatCompletionFunctionToolParam = {
    "type": "function",
    "function": {
        "name": "submit_report",
        "description": "提交初审建议。只能在校验执行完成且没有缺失字段后调用。",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "初审结论说明"},
                "open_items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "仍需人工确认的事项, 没有则给空数组",
                },
            },
            "required": ["summary", "open_items"],
            "additionalProperties": False,
        },
    },
}

_TOOLS = [
    READ_MATERIAL_TOOL,
    CHECK_MATERIALS_TOOL,
    SEARCH_POLICY_TOOL,
    ASK_USER_TOOL,
    SUBMIT_REPORT_TOOL,
]
_TOOL_NAMES = {tool["function"]["name"] for tool in _TOOLS}

# 引用标签的字符形态: 版本键@定位, 定位到标点或空白为止
_CITATION_LABEL = re.compile(
    r"(?P<edition>[a-z][a-z0-9_]*)@(?P<locator>[^\s\uff0c\u3002\uff1b\u3001\uff08\uff09()\[\]{}"
    r"\u300c\u300d\u3010\u3011\"']+)"
)

# 材料页来源: 材料ID@page:N。ID 允许大小写与连字符, 与 materials.py 的定位格式一致
_MATERIAL_SOURCE_LABEL = re.compile(r"^[A-Za-z0-9_\-]+@page:\d+$")
# 用户补充来源: 用户补充@R<轮次>
_SUPPLEMENT_SOURCE_LABEL = re.compile(r"^用户补充@R\d+$")

# 明确的准入结论措辞, 用于机械拦截越权表述
_APPROVAL_PHRASES = (
    "准入通过",
    "批准准入",
    "准入批准",
    "建议批准",
    "审核通过",
    "审批通过",
    "准予准入",
    "同意准入",
    "通过准入",
)
# 命中词之前的否定: 紧邻否定词覆盖"未通过""不予批准"
_NEGATIONS = ("不", "非", "未", "无", "别", "勿")
# 免责习语: "不构成供应商准入批准""不作为准入批准"等, 中间隔了若干字
_NEGATION_IDIOMS = ("不构成", "不作为", "不代表", "不等于", "不算作")


class ToolArgumentError(ValueError):
    """模型提交的工具参数非法时抛出的错误, 消息需指明具体字段。"""


class _ToolDenied(Exception):
    """工具错误已入档, 且纠正机会已用尽, 本轮应以失败结束。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ReviewOutcome(BaseModel):
    """一次审查会话的最终结果与运行观测。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["report", "question", "failed"]
    text: str
    rounds: int
    model_requests: int
    tool_attempts: int
    elapsed_seconds: float
    prompt_tokens: int
    completion_tokens: int
    reference_date: date
    material_ids: list[str]
    policy_version: str
    rule_results: list[dict[str, Any]] = []
    citations: list[dict[str, Any]] = []
    tool_events: list[dict[str, Any]] = []
    supplements: list[dict[str, Any]] = []


def _parse_object_arguments(raw_json: str, *, tool: str) -> dict[str, Any]:
    """把模型提交的参数解析为 JSON 对象, 坏 JSON 归一为工具参数错误。"""

    try:
        raw = json.loads(raw_json)
    except (TypeError, ValueError) as exc:
        raise ToolArgumentError(f"{tool} 参数不是合法 JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ToolArgumentError(f"{tool} 参数必须是 JSON 对象")
    return raw


def _parse_extracted_facts(raw_json: str) -> ExtractedFacts:
    """解析 check_materials 的提取层参数。"""

    raw = _parse_object_arguments(raw_json, tool="check_materials")
    try:
        return ExtractedFacts(**raw)
    except Exception as exc:
        raise ToolArgumentError(f"提取事实未通过 Schema 校验: {exc}") from exc


def _parse_ask_user(raw_json: str) -> str:
    """解析追问参数, 只接受非空的 prompt 字段。"""

    raw = _parse_object_arguments(raw_json, tool="ask_user")
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ToolArgumentError("ask_user 需要非空的 prompt 字段")
    return prompt.strip()


def _parse_submit_report(raw_json: str) -> tuple[str, list[str]]:
    """解析报告参数, 摘要必需, 待确认事项缺省为空列表。"""

    raw = _parse_object_arguments(raw_json, tool="submit_report")
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ToolArgumentError("submit_report 需要非空的 summary 字段")
    open_items = raw.get("open_items", [])
    if not isinstance(open_items, list) or any(not isinstance(item, str) for item in open_items):
        raise ToolArgumentError("submit_report 的 open_items 必须是字符串数组")
    return summary.strip(), [item for item in open_items if item.strip()]


def _citation_labels(citations: Sequence[Mapping[str, Any]]) -> set[str]:
    """把检索结果渲染为可核对的引用标签集合。"""

    labels: set[str] = set()
    for citation in citations:
        locator = citation.get("locator", ())
        if isinstance(locator, (list, tuple)):
            labels.add(f"{citation['edition_key']}@{'/'.join(str(part) for part in locator)}")
    return labels


def _is_program_source_label(label: str) -> bool:
    """判断标签是否指向程序掌握的材料页或用户补充轮次。

    这两类不是制度出处, 但模型在叙述依据时合法引用它们, 因此不该按
    "编造出处"处理。它们本身另有校验: 材料页由 require_literal_sources
    逐字段核对, 补充轮次由程序登记, 模型伪造不出不存在的页或轮次。
    """

    return bool(_MATERIAL_SOURCE_LABEL.match(label) or _SUPPLEMENT_SOURCE_LABEL.match(label))


def verify_citation_labels(summary: str, *, allowed: set[str]) -> list[str]:
    """检查报告文本里出现的引用标签是否都来自本次检索返回。

    只匹配形如 `edition_key@定位` 的串, 普通叙述不受影响; 编造出处的标签
    会被列出来交给纠正流程, 而不是直接写入报告。中文正文没有词间空格,
    因此按字符形态匹配而不是切词。

    材料页与用户补充来源不算编造: 只允许检索命中集合会把 `材料ID@page:1`
    这类合法引用判为伪造。实测中出现过这种误报, 连续拒绝两次导致整次运行
    失败, 因此这里按来源形态放行。
    """

    forged: list[str] = []
    for match in _CITATION_LABEL.finditer(summary):
        label = f"{match.group('edition')}@{match.group('locator')}"
        if label in allowed or label in forged:
            continue
        if _is_program_source_label(label):
            continue
        forged.append(label)
    return sorted(forged)


def find_approval_claims(summary: str) -> list[str]:
    """找出报告叙述里宣告准入结论的措辞。

    提示词要求"规则未命中不等于准入批准", 但模型仍可能写出"准入通过""建议批准"
    这类越权表述——只靠提示词约束不住, 与 M1 的结论出口问题是同一类。这里
    用机械检查把它挡在成稿之前, 命中即要求模型改写, 而不是写入报告。

    被否定的表述不算宣告: "不构成准入批准"是免责声明, "未通过准入"是相反
    结论, 两者都不该触发改写, 否则正常报告会被反复拒绝。因此命中词前若紧邻
    否定词则跳过。只匹配明确的准入结论词, 不对"规则未命中""材料齐备"误报。
    """

    claims: set[str] = set()
    for phrase in _APPROVAL_PHRASES:
        for match in re.finditer(re.escape(phrase), summary):
            if _is_negated(summary, match.start()):
                continue
            claims.add(phrase)
    return sorted(claims)


def _is_negated(summary: str, start: int) -> bool:
    """判断命中词是否被否定, 用于排除免责声明与相反结论。

    两种情形算被否定: 否定词紧邻命中词("未通过准入"), 或前面出现免责习语
    ("不构成供应商准入批准")。刻意不做大窗口的模糊匹配——"材料不齐全,
    建议批准准入"里的"不"并不否定结论, 用宽窗口会漏报真实越权。
    """

    adjacent = summary[max(0, start - 2) : start]
    if any(negation in adjacent for negation in _NEGATIONS):
        return True
    window = summary[max(0, start - 8) : start]
    return any(idiom in window for idiom in _NEGATION_IDIOMS)


def _render_question(question: str, *, result: MaterialCheckResult | None) -> str:
    """渲染追问文本, 并在已校验时附上程序已确认的结论。

    只让用户看到问题是不够的: 材料可能既有已查出的问题(如执照已过期),
    又有读不到的信息。已确认部分由程序渲染, 保证用户看到的问题不会因为
    模型选择追问而被隐去。
    """

    lines: list[str] = []
    if result is not None:
        hits = [
            evaluation
            for evaluation in result.evaluations
            if evaluation.result == "hit" and evaluation.outcome is not None
        ]
        if hits:
            lines.append("已完成校验, 已发现的处置事项:")
            for evaluation in hits:
                outcome = evaluation.outcome
                action = outcome.action if outcome is not None else None
                reason = outcome.reason_code if outcome is not None else None
                lines.append(f"- {evaluation.rule_id}: {action} ({reason})")
            lines.append("")
    lines.append("需要补充的信息:")
    lines.append(question)
    return "\n".join(lines)


def _render_report(
    *,
    summary: str,
    open_items: list[str],
    result: MaterialCheckResult | None,
    citations: Sequence[Mapping[str, Any]],
    reference_date: date,
    material_ids: Sequence[str],
) -> str:
    """把模型结论与**程序核对过的**规则结果、引用拼成最终报告。

    规则结论与引用块由程序渲染而不是由模型书写, 因此报告里的规则命中
    与出处都可回溯到实际执行结果; 模型只负责叙述部分。
    """

    lines = ["# 供应商材料初审报告", ""]
    lines.append(f"- 参考日期: {reference_date.isoformat()}")
    lines.append(f"- 材料: {', '.join(material_ids)}")
    if result is not None:
        lines.append(f"- 规则版本: {result.policy_version}")
    lines.append("")
    lines.append("## 初审建议")
    lines.append(summary)
    lines.append("")

    if result is not None:
        lines.append("## 规则校验结果(程序执行)")
        lines.append("| 规则 | 结果 | 建议动作 |")
        lines.append("| --- | --- | --- |")
        for evaluation in result.evaluations:
            action = evaluation.outcome.action if evaluation.outcome is not None else "—"
            lines.append(f"| {evaluation.rule_id} | {evaluation.result} | {action} |")
        if result.missing_fields:
            lines.append("")
            lines.append(f"- 缺失字段: {', '.join(result.missing_fields)}")
        if result.derived_business_license_status is not None:
            lines.append(
                f"- 营业执照状态由程序按参考日期派生: {result.derived_business_license_status}"
            )
        lines.append("")

    lines.append("## 制度依据(检索命中)")
    if citations:
        for citation in citations:
            locator = citation.get("locator", ())
            label = f"{citation['edition_key']}@{'/'.join(str(p) for p in locator)}"
            lines.append(f"- {label}")
            body = str(citation.get("body", "")).strip().replace("\n", " ")
            lines.append(f"  > {body[:200]}")
    else:
        lines.append("- 本次未检索到可引用的制度片段, 相关判断需人工确认。")
    lines.append("")

    lines.append("## 待人工确认")
    lines.extend(f"- {item}" for item in open_items) if open_items else lines.append("- 无")
    lines.append("")
    lines.append("> 本报告为初审建议, 不构成供应商准入批准; 最终决定由有权限的人工审批人作出。")
    return "\n".join(lines)


class _ReviewLoop:
    """单轮审查循环: 状态、预算与各工具的处理。

    每个 `_handle_*` 返回 `ReviewOutcome` 表示本轮结束, 返回 `None` 表示
    记录结果后继续循环。需要终止时经 `_deny` 抛出 `_ToolDenied`。
    """

    def __init__(
        self,
        *,
        user_request: str,
        session: ReviewSession,
        policy: PolicyDocument,
        index: KnowledgeIndex,
        client: OpenAI,
        model_name: str,
        reference_date: date,
        max_model_requests: int,
        max_tool_attempts: int,
        request_timeout: float,
        total_timeout: float,
    ) -> None:
        self.session = session
        self.policy = policy
        self.index = index
        self.client = client
        self.model_name = model_name
        self.reference_date = reference_date
        self.max_model_requests = max_model_requests
        self.max_tool_attempts = max_tool_attempts
        self.request_timeout = request_timeout
        self.total_timeout = total_timeout

        self.started = time.monotonic()
        session.begin_round()
        materials_note = ", ".join(session.sources.materials) or "(无)"
        self.messages: list[ChatCompletionMessageParam] = [
            ChatCompletionSystemMessageParam(role="system", content=SYSTEM_PROMPT),
            ChatCompletionUserMessageParam(
                role="user",
                content=(
                    f"本次提交的材料 ID: {materials_note}。"
                    f"参考日期: {reference_date.isoformat()}。请求: {user_request}"
                ),
            ),
        ]
        for line in session.history_lines():
            self.messages.append(ChatCompletionUserMessageParam(role="user", content=line))

        self.model_requests = 0
        self.tool_attempts = 0
        self.corrections_used = 0
        self.ask_correction_used = False
        self.checked = False
        self.read_done = False
        self.searched = False
        self.last_check: MaterialCheckResult | None = None
        self.citation_pool: list[dict[str, Any]] = []
        self.rule_results: list[dict[str, Any]] = []
        self.tool_events: list[dict[str, Any]] = []
        self.prompt_tokens = 0
        self.completion_tokens = 0

    # ---- 状态与出口 -------------------------------------------------------

    def finish(self, kind: Literal["report", "question", "failed"], text: str) -> ReviewOutcome:
        """构造带完整观测字段的结果对象。"""

        return ReviewOutcome(
            kind=kind,
            text=_redact_secret(text),
            rounds=self.session.round_number,
            model_requests=self.model_requests,
            tool_attempts=self.tool_attempts,
            elapsed_seconds=time.monotonic() - self.started,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            reference_date=self.reference_date,
            material_ids=sorted(self.session.sources.materials),
            policy_version=self.policy.version,
            rule_results=self.rule_results,
            citations=self.citation_pool,
            tool_events=list(self.tool_events),
            supplements=[record.model_dump() for record in self.session.supplements],
        )

    def note(self, name: str, status: str, detail: object, arguments: str | None = None) -> None:
        """记录一条工具事件, 供运行记录与测试断言。"""

        self.tool_events.append(
            {"name": name, "status": status, "detail": detail, "arguments": arguments}
        )

    def _deny(
        self,
        detail: str,
        *,
        bucket: Literal["shared", "ask"],
        tool_name: str,
        call_id: str,
        arguments: str | None,
    ) -> None:
        """记录一次工具错误, 循环继续让模型纠正; 纠正用尽时抛出 `_ToolDenied`。

        正常返回意味着"错误已作为工具结果回传, 循环继续"; 抛出意味着连纠正
        机会也没有了, 本轮以失败结束。调用方直接调用本方法即可, 无需检查返回值。
        """

        if bucket == "ask":
            if self.ask_correction_used:
                self.note("ask_user", "error", f"专属纠正机会已用尽: {detail}", arguments)
                raise _ToolDenied(f"追问参数非法, 专属纠正机会已用尽: {detail}")
            self.ask_correction_used = True
        else:
            if self.corrections_used >= 1:
                self.note(tool_name, "error", f"纠正机会已用尽: {detail}", arguments)
                raise _ToolDenied(f"纠正机会已用尽: {detail}")
            self.corrections_used += 1
        self.note(tool_name, "error", detail, arguments)
        self.messages.append(
            ChatCompletionToolMessageParam(
                role="tool",
                tool_call_id=call_id,
                content=json.dumps({"error": detail}, ensure_ascii=False),
            )
        )

    # ---- 主循环 -----------------------------------------------------------

    def run(self, user_request: str) -> ReviewOutcome:
        """驱动循环直到产出报告、追问或明确失败。"""

        while True:
            remaining = self.total_timeout - (time.monotonic() - self.started)
            if remaining <= 0:
                return self.finish("failed", f"总时限 {self.total_timeout} 秒已用尽")
            if self.model_requests >= self.max_model_requests:
                return self.finish("failed", f"本轮模型请求达到上限 {self.max_model_requests} 次")
            self.model_requests += 1

            try:
                completion = self.client.chat.completions.create(
                    model=self.model_name,
                    messages=self.messages,
                    tools=_TOOLS,
                    timeout=min(self.request_timeout, max(remaining, 0.001)),
                    extra_body={"enable_thinking": False},
                )
            except Exception as exc:
                return self.finish("failed", f"模型请求失败: {exc}")

            if completion.usage is not None:
                self.prompt_tokens += completion.usage.prompt_tokens
                self.completion_tokens += completion.usage.completion_tokens
            if not completion.choices:
                return self.finish("failed", "模型响应缺少 choices, 可能被内容过滤拦截")

            message = completion.choices[0].message
            self.messages.append(
                cast(
                    ChatCompletionAssistantMessageParam,
                    message.model_dump(exclude_none=True),
                )
            )
            calls = message.tool_calls or []

            if not calls:
                outcome = self._handle_text_only(
                    text=(message.content or "").strip(),
                    finish_reason=completion.choices[0].finish_reason,
                )
            elif len(calls) > 1:
                outcome = self._handle_multiple_calls(calls)
            else:
                call = calls[0]
                if self.tool_attempts >= self.max_tool_attempts:
                    return self.finish(
                        "failed", f"工具调用尝试达到上限 {self.max_tool_attempts} 次"
                    )
                self.tool_attempts += 1
                if call.type != "function":
                    return self.finish("failed", f"不支持的工具调用类型: {call.type}")
                try:
                    outcome = self._dispatch(call.function.name, call.id, call.function.arguments)
                except _ToolDenied as denied:
                    return self.finish("failed", denied.reason)

            if outcome is not None:
                return outcome

    def _handle_text_only(self, *, text: str, finish_reason: str) -> ReviewOutcome | None:
        """处理没有工具调用的响应: 纯文本不能作为任何出口。"""

        if finish_reason != "stop":
            return self.finish("failed", f"回答被截断或过滤: {finish_reason}")
        if not text:
            return self.finish("failed", "模型返回空回答")
        self.note("<text>", "error", "纯文本不能作为结论出口", text[:200])
        if self.corrections_used >= 1:
            return self.finish("failed", "未调用工具就给出结论, 且纠正机会已用尽")
        self.corrections_used += 1
        self.messages.append(
            ChatCompletionUserMessageParam(
                role="user",
                content=(
                    "结论必须经 submit_report 工具给出; "
                    "需要补充信息请调用 ask_user; 已完成的步骤不要重复。"
                ),
            )
        )
        return None

    def _handle_multiple_calls(self, calls: Sequence[Any]) -> ReviewOutcome | None:
        """一次响应限定一个工具调用, 避免"追问与执行并发"的含混行为。"""

        self.tool_attempts += len(calls)
        for bad in calls:
            name = bad.function.name if bad.type == "function" else f"<type:{bad.type}>"
            self.note(name, "error", "一次响应只允许一个工具调用", None)
            self.messages.append(
                ChatCompletionToolMessageParam(
                    role="tool",
                    tool_call_id=bad.id,
                    content=json.dumps({"error": "一次响应只允许一个工具调用"}, ensure_ascii=False),
                )
            )
        if self.corrections_used >= 1:
            return self.finish("failed", "纠正机会已用尽, 模型仍返回多个工具调用")
        self.corrections_used += 1
        return None

    def _dispatch(self, name: str, call_id: str, arguments: str) -> ReviewOutcome | None:
        """把一次工具调用路由到对应处理函数。"""

        if name not in _TOOL_NAMES:
            self.note(name, "error", f"未知工具: {name}", arguments)
            if self.corrections_used >= 1:
                return self.finish("failed", f"纠正机会已用尽, 再次返回未知工具: {name}")
            self.corrections_used += 1
            self.messages.append(
                ChatCompletionToolMessageParam(
                    role="tool",
                    tool_call_id=call_id,
                    content=json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False),
                )
            )
            return None

        handlers = {
            "read_material": self._handle_read_material,
            "check_materials": self._handle_check_materials,
            "search_policy": self._handle_search_policy,
            "ask_user": self._handle_ask_user,
            "submit_report": self._handle_submit_report,
        }
        return handlers[name](call_id, arguments)

    # ---- 各工具处理 -------------------------------------------------------

    def _handle_read_material(self, call_id: str, arguments: str) -> ReviewOutcome | None:
        """按材料 ID 返回逐页原文; 白名单之外的材料一律拒绝。"""

        try:
            args = _parse_object_arguments(arguments, tool="read_material")
        except ToolArgumentError as exc:
            self._deny(
                str(exc),
                bucket="shared",
                tool_name="read_material",
                call_id=call_id,
                arguments=arguments,
            )
            return None

        material_id = args.get("material_id")
        material = self.session.sources.materials.get(str(material_id))
        if material is None:
            self._deny(
                f"本次提交中没有材料 {material_id}; 可选: {sorted(self.session.sources.materials)}",
                bucket="shared",
                tool_name="read_material",
                call_id=call_id,
                arguments=arguments,
            )
            return None

        self.read_done = True
        self.note("read_material", "ok", {"material_id": material.material_id}, arguments)
        self.messages.append(
            ChatCompletionToolMessageParam(
                role="tool",
                tool_call_id=call_id,
                content=json.dumps(
                    {
                        "material_id": material.material_id,
                        "page_count": material.page_count,
                        "pages": [
                            {"page": index, "text": text}
                            for index, text in enumerate(material.pages, start=1)
                        ],
                    },
                    ensure_ascii=False,
                ),
            )
        )
        return None

    def _handle_check_materials(self, call_id: str, arguments: str) -> ReviewOutcome | None:
        """核对来源后执行规则; 核对失败时任何规则都不执行。"""

        if not self.read_done:
            self._deny(
                "必须先调用 read_material 读取材料原文",
                bucket="shared",
                tool_name="check_materials",
                call_id=call_id,
                arguments=arguments,
            )
            return None

        try:
            facts = _parse_extracted_facts(arguments)
            result = check_extracted_facts(
                facts,
                policy=self.policy,
                sources=self.session.sources,
                reference_date=self.reference_date,
            )
        except (ExtractionError, ToolArgumentError) as exc:
            self._deny(
                str(exc),
                bucket="shared",
                tool_name="check_materials",
                call_id=call_id,
                arguments=arguments,
            )
            return None
        except Exception as exc:
            self.note("check_materials", "error", f"工具内部错误: {exc}", arguments)
            return self.finish("failed", f"工具内部错误: {exc}")

        self.checked = True
        self.last_check = result
        self.rule_results = [item.model_dump() for item in result.evaluations]
        self.note("check_materials", "ok", result.model_dump(mode="json"), arguments)
        self.messages.append(
            ChatCompletionToolMessageParam(
                role="tool",
                tool_call_id=call_id,
                content=result.model_dump_json(),
            )
        )
        trailer = (
            "missing_fields 非空时, 必须调用 ask_user 追问缺失信息, 不得自行假设或填入猜测值。"
            if result.missing_fields
            else "校验已完成且无缺失字段, 可继续检索依据并提交报告。"
        )
        self.messages.append(
            ChatCompletionUserMessageParam(
                role="user",
                content=(
                    "校验结果中的 action 与 target_case_status 只是建议动作, "
                    "只能描述为建议, 不得声称系统已执行或已改变案件状态。" + trailer
                ),
            )
        )
        return None

    def _handle_search_policy(self, call_id: str, arguments: str) -> ReviewOutcome | None:
        """按参考日期检索现行制度; 命中片段进入本轮可引用池。"""

        try:
            args = _parse_object_arguments(arguments, tool="search_policy")
            query = str(args.get("query", "")).strip()
            if not query:
                raise ToolArgumentError("search_policy 需要非空 query")
            hit = self.index.search(query, as_of=self.reference_date)
        except (ToolArgumentError, RetrievalError) as exc:
            self._deny(
                str(exc),
                bucket="shared",
                tool_name="search_policy",
                call_id=call_id,
                arguments=arguments,
            )
            return None

        self.searched = True
        payload = hit.model_dump(mode="json")
        for citation in payload["citations"]:
            if citation not in self.citation_pool:
                self.citation_pool.append(citation)
        self.note("search_policy", "ok", payload, arguments)
        self.messages.append(
            ChatCompletionToolMessageParam(
                role="tool",
                tool_call_id=call_id,
                content=json.dumps(payload, ensure_ascii=False),
            )
        )
        if not payload["citations"]:
            self.messages.append(
                ChatCompletionUserMessageParam(
                    role="user",
                    content=(
                        "本次检索没有命中片段。不要凭记忆回答; "
                        "可以换关键词再检索, 或把该问题列为待人工确认事项。"
                    ),
                )
            )
        return None

    def _handle_ask_user(self, call_id: str, arguments: str) -> ReviewOutcome | None:
        """把追问作为本轮出口; 追问参数错误走专属纠正桶。"""

        try:
            question = _parse_ask_user(arguments)
        except ToolArgumentError as exc:
            self._deny(
                str(exc),
                bucket="ask",
                tool_name="ask_user",
                call_id=call_id,
                arguments=arguments,
            )
            return None

        self.note("ask_user", "ok", {"question": question}, arguments)
        # 追问时一并给出程序已确认的结论: 否则用户只看到问题, 看不到
        # 已经查出的问题(例如执照已过期). 这段由程序渲染, 不依赖模型转述.
        return self.finish("question", _render_question(question, result=self.last_check))

    def _handle_submit_report(self, call_id: str, arguments: str) -> ReviewOutcome | None:
        """报告出口的三道前置检查: 已校验、无缺失、引用与措辞合规。"""

        if not self.checked:
            self._deny(
                "必须先执行 check_materials 校验再提交报告",
                bucket="shared",
                tool_name="submit_report",
                call_id=call_id,
                arguments=arguments,
            )
            return None
        if self.last_check is not None and self.last_check.missing_fields:
            self._deny(
                "仍有缺失字段 "
                f"{self.last_check.missing_fields}, 必须先用 ask_user 追问, 不得直接给出结论",
                bucket="shared",
                tool_name="submit_report",
                call_id=call_id,
                arguments=arguments,
            )
            return None

        try:
            summary, open_items = _parse_submit_report(arguments)
        except ToolArgumentError as exc:
            self._deny(
                str(exc),
                bucket="shared",
                tool_name="submit_report",
                call_id=call_id,
                arguments=arguments,
            )
            return None

        forged = verify_citation_labels(summary, allowed=_citation_labels(self.citation_pool))
        if forged:
            self._deny(
                f"以下引用不在本次检索结果中, 不得编造出处: {', '.join(forged)}",
                bucket="shared",
                tool_name="submit_report",
                call_id=call_id,
                arguments=arguments,
            )
            return None

        claims = find_approval_claims(summary)
        if claims:
            self._deny(
                "报告叙述不得宣告准入结论, 命中措辞: "
                f"{', '.join(claims)}。规则未命中不等于准入批准, "
                "只能描述检查结果与建议动作, 最终决定由人工审批人作出。",
                bucket="shared",
                tool_name="submit_report",
                call_id=call_id,
                arguments=arguments,
            )
            return None

        if not self.searched:
            self.messages.append(
                ChatCompletionUserMessageParam(
                    role="user",
                    content="尚未检索制度依据; 结论需有依据支撑, 建议先调用 search_policy。",
                )
            )

        self.note("submit_report", "ok", {"summary": summary, "open_items": open_items}, None)
        return self.finish(
            "report",
            _render_report(
                summary=summary,
                open_items=open_items,
                result=self.last_check,
                citations=self.citation_pool,
                reference_date=self.reference_date,
                material_ids=sorted(self.session.sources.materials),
            ),
        )


def run_review(
    user_request: str,
    *,
    session: ReviewSession,
    policy: PolicyDocument,
    index: KnowledgeIndex,
    client: OpenAI,
    model_name: str,
    reference_date: date = DEFAULT_REFERENCE_DATE,
    max_model_requests: int = 8,
    max_tool_attempts: int = 12,
    request_timeout: float = 60.0,
    total_timeout: float = 180.0,
) -> ReviewOutcome:
    """驱动一次有预算的完整审查会话, 返回报告、追问或明确失败。

    每一轮都复用同一套循环: 预算按轮重置(与 PRD"每次用户提交"一致),
    会话层面的轮次上限由 `ReviewSession` 掌握。追问时返回 kind=question,
    调用方补充后再次调用本函数即可在同一会话上继续。
    """

    loop = _ReviewLoop(
        user_request=user_request,
        session=session,
        policy=policy,
        index=index,
        client=client,
        model_name=model_name,
        reference_date=reference_date,
        max_model_requests=max_model_requests,
        max_tool_attempts=max_tool_attempts,
        request_timeout=request_timeout,
        total_timeout=total_timeout,
    )
    return loop.run(user_request)


def write_review_log(
    outcome: ReviewOutcome,
    *,
    model_name: str,
    log_dir: Path,
    started_at: datetime,
    user_request: str,
    materials: Sequence[Mapping[str, Any]],
) -> Path:
    """把一次审查运行落盘为脱敏 JSON, 返回写入的文件路径。

    除 M1 已有的模型标识, 工具事件, 耗时与 token 用量外, 另记录材料文件名
    与 sha256、参考日期、每轮用户补充原文, 以及每个事实值的来源与核对结果,
    使"来源页码能核对"可被第三方复核。不写 API Key, 并对 sk- 样式密钥兜底打码。
    """

    log_dir.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "started_at": started_at.isoformat(timespec="seconds"),
        "model": model_name,
        "user_request": user_request,
        "simulated_input": False,
        "kind": outcome.kind,
        "text": outcome.text,
        "rounds": outcome.rounds,
        "reference_date": outcome.reference_date.isoformat(),
        "policy_version": outcome.policy_version,
        "materials": [dict(item) for item in materials],
        "supplements": outcome.supplements,
        "rule_results": outcome.rule_results,
        "citations": outcome.citations,
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


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口: python -m vendorguard.review <材料PDF...> [选项].

    返回码: 0 产出报告或追问, 1 运行失败, 2 启动配置错误。

    `--supplement` 是主要的可复现路径: 补充内容脚本化, 运行可重复、可入档;
    追问后未提供补充时进入交互式输入, 供手工探索。
    """

    import argparse
    import os
    from datetime import datetime

    from dotenv import dotenv_values

    from .agent import AgentConfigError, load_llm_settings
    from .materials import MaterialReadError, SourceTexts, read_material
    from .policy import load_policy
    from .retrieval import load_knowledge_index

    parser = argparse.ArgumentParser(
        prog="vendorguard.review", description="M2+M3 材料审查完整链路"
    )
    parser.add_argument("materials", nargs="+", type=Path, help="材料 PDF 路径(可多份)")
    parser.add_argument(
        "--request",
        default="请审查这份供应商准入材料。",
        help="审查请求文本",
    )
    parser.add_argument("--supplement", action="append", default=[], help="预先给定的用户补充")
    parser.add_argument("--reference-date", default=DEFAULT_REFERENCE_DATE.isoformat())
    args = parser.parse_args(argv)

    started_at = datetime.now()
    try:
        settings = load_llm_settings({**dotenv_values(".env"), **os.environ})
        policy = load_policy(Path("policies/rules/v1.0.0.yaml"))
        index = load_knowledge_index(Path("data/knowledge"))
    except (AgentConfigError, RetrievalError) as exc:
        print(f"启动失败: {exc}")
        return 2

    try:
        reference_date = date.fromisoformat(args.reference_date)
    except ValueError as exc:
        print(f"启动失败: 参考日期格式非法: {exc}")
        return 2

    try:
        materials = [read_material(path) for path in args.materials]
    except MaterialReadError as exc:
        print(f"[材料读取失败:{exc.code}] {exc}")
        return 1

    session = ReviewSession(sources=SourceTexts.from_materials(materials))
    client = OpenAI(
        api_key=settings.api_key, base_url=settings.base_url, timeout=60.0, max_retries=0
    )

    pending = list(args.supplement)
    outcome: ReviewOutcome | None = None
    while True:
        try:
            outcome = run_review(
                args.request,
                session=session,
                policy=policy,
                index=index,
                client=client,
                model_name=settings.model_name,
                reference_date=reference_date,
            )
        except SessionError as exc:
            print(f"[会话结束] {exc}")
            break

        if outcome.kind != "question":
            break
        print(f"[追问] {outcome.text}")
        if pending:
            supplement = pending.pop(0)
        else:
            try:
                supplement = input("请补充(直接回车结束): ").strip()
            except EOFError:
                supplement = ""
            if not supplement:
                print("未提供补充, 会话结束。")
                break
        record = session.record_supplement(supplement)
        print(f"[已登记] {record.as_locator()}: {record.text}")

    if outcome is None:
        return 1

    labels = {"report": "初审报告", "question": "追问", "failed": "运行失败"}
    print(f"[{labels[outcome.kind]}]")
    print(outcome.text)
    log_path = write_review_log(
        outcome,
        model_name=settings.model_name,
        log_dir=Path("logs/agent-runs"),
        started_at=started_at,
        user_request=args.request,
        materials=[
            {
                "material_id": item.material_id,
                "source_name": item.source_name,
                "sha256": item.sha256,
                "page_count": item.page_count,
            }
            for item in materials
        ],
    )
    print(f"运行记录: {log_path}")
    return 0 if outcome.kind in ("report", "question") else 1


__all__ = [
    "DEFAULT_REFERENCE_DATE",
    "ReviewOutcome",
    "ToolArgumentError",
    "find_approval_claims",
    "main",
    "run_review",
    "verify_citation_labels",
    "write_review_log",
]


if __name__ == "__main__":
    raise SystemExit(main())
