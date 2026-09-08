"""C2 协议探针(v2): 经 OpenAI 兼容协议强制 qwen3.7-flash 调用 check_materials.

背景: 原生 DashScope SDK 的 Generation.call 在本账号本模型上返回
InvalidParameter url error(两种地域端点均复现), 改用百炼官方
OpenAI 兼容协议. 本版仍只发一次请求, 打印原始结构, 不解析业务字段.
模拟输入: 事实为手工构造的结构化样例, 不代表真实 PDF.
"""

import json
from pathlib import Path

from dotenv import dotenv_values
from openai import OpenAI

env = dotenv_values(Path(".env"))
base_url = env["VENDORGUARD_LLM_BASE_URL"]

# 本地预检: 端点家族不对就零成本退出, 不发出请求。
if not base_url.endswith("/compatible-mode/v1"):
    raise SystemExit(f"端点结尾不是 /compatible-mode/v1, 当前末段: {base_url.rsplit('/', 2)}")

client = OpenAI(
    api_key=env["VENDORGUARD_LLM_API_KEY"],
    base_url=base_url,
    timeout=30.0,
    max_retries=0,  # 方案预算要求: 关闭 SDK 自动重试
)

CHECK_MATERIALS_TOOL = {
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

submitted = {
    "business_license_document_status": "valid",
    "category_required_documents_complete": True,
    "sources": {
        "business_license_document_status": "BL-001@page_1",
        "category_required_documents_complete": "CHECKLIST-001@row_2",
    },
}

messages = [
    {
        "role": "system",
        "content": (
            "你是供应商材料审查助手. 检查供应商材料是否满足准入要求时, "
            "必须调用 check_materials 工具, 参数从用户提交的事实原样取值, 不得改写. "
            "回答时忠实转述工具结果: 规则未命中不等于准入批准, 不得自行宣布通过准入."
        ),
    },
    {
        "role": "user",
        "content": (
            "请检查这家供应商的材料是否满足准入要求. "
            "本次提交的事实(模拟输入): " + json.dumps(submitted, ensure_ascii=False)
        ),
    },
]

# --- 第 1 轮: 强制工具调用, 拿到参数 ---
first = client.chat.completions.create(
    model=env["VENDORGUARD_LLM_MODEL"],
    messages=messages,
    tools=[CHECK_MATERIALS_TOOL],
    tool_choice={"type": "function", "function": {"name": "check_materials"}},
    extra_body={"enable_thinking": False},
)
call = first.choices[0].message.tool_calls[0]
print("第 1 轮 arguments:", call.function.arguments)

from vendorguard.agent_tools import ToolArgumentError, check_materials, parse_tool_arguments
from vendorguard.policy import StructuredFacts, load_policy
from pathlib import Path

policy = load_policy(Path("policies/rules/v1.0.0.yaml"))
snapshot = StructuredFacts(**submitted)

try:
    facts = parse_tool_arguments(call.function.arguments)
    result = check_materials(facts, policy=policy, submitted=snapshot)
    tool_content = result.model_dump_json()
except ToolArgumentError as exc:
    # 结构化错误也照实回传, 让模型看到被拒绝的原因。
    tool_content = json.dumps({"error": str(exc)}, ensure_ascii=False)
print("工具真实执行结果:", tool_content)

# --- 第 2 轮: 回传结果, 模型消费并回答 ---
messages.append(first.choices[0].message.model_dump(exclude_none=True))
messages.append(
    {
        "role": "tool",
        "tool_call_id": call.id,
        "name": "check_materials",
        "content": tool_content,
    }
)

second = client.chat.completions.create(
    model=env["VENDORGUARD_LLM_MODEL"],
    messages=messages,
    tools=[CHECK_MATERIALS_TOOL],
    extra_body={"enable_thinking": False},
)
final = second.choices[0]
print("第 2 轮 finish_reason:", final.finish_reason)
print("模型最终回答:", final.message.content)