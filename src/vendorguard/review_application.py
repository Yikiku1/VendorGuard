"""审查应用层: CLI 与 HTTP 共用同一条审查执行链.

M4 方案 4.1: "装载依赖并执行一轮"只在这一处接线. CLI 与 HTTP 都调它, 而 Agent 的
工具循环仍然只有 `agent.run_review()` 一处; 本模块不复制工具定义, 纠正预算, 检索
白名单或报告闸门, 也不认识 HTTP, 不写文件, 不碰数据库.

每次调用 `run_review_round()` 都按 command 新建 `ReviewSession`: 一轮运行只认它自己
那份材料与补充原文, 上一轮的检索节点与校验结论不会顺延 (M3-6 的结论).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from openai import OpenAI
from pydantic import BaseModel, ConfigDict

from .agent import (
    POLICY_CHUNK_LIST,
    AgentConfigError,
    AgentRunOutcome,
    PolicyIndex,
    load_llm_settings,
    load_policy_index,
    run_review,
)
from .agent_session import ReviewSession
from .config import Settings
from .materials import MaterialDocument
from .policy import PolicyDocument, PolicyLoadError, load_policy

# 现行规则文件: 与片段清单一样是"这一版规则与制度"的固定输入, 由启动入口装载一次.
RULES_PATH = Path("policies/rules/v1.0.0.yaml")
# 模型客户端在进程内只建一次; 单次请求超时与 M3 的 CLI 保持一致.
REQUEST_TIMEOUT = 30.0


class ReviewCommand(BaseModel):
    """一次审查运行的输入: 用户要求, 参考日期, 一份材料与已登记的用户补充.

    补充原文是用户说过的话, 由调用方按"最多一次补充"的规则放进来; 应用层只负责
    把它登记成会话来源, 不判断该不该有补充.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_request: str
    reference_date: date
    material: MaterialDocument
    supplements: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewRuntime:
    """一次进程内共用的审查依赖: 模型客户端, 规则, 检索数据与模型名.

    装载一次后所有轮次复用; 测试直接传替身. 这里用 frozen dataclass 而不是 pydantic
    模型: client 是活对象 (真实 OpenAI 客户端或替身), 既不需要字段校验, 也不该在
    构造时按 OpenAI 做 isinstance 检查.
    """

    client: OpenAI
    policy: PolicyDocument
    policy_index: PolicyIndex
    model_name: str


def build_review_runtime(
    env: Mapping[str, str | None],
    *,
    app_settings: Settings,
    rules_path: Path = RULES_PATH,
    chunk_list_path: Path = POLICY_CHUNK_LIST,
) -> ReviewRuntime:
    """装载一轮审查需要的全部依赖, 缺任何一项都以启动失败收尾.

    缺模型配置时一次报全所有缺失的环境变量名; 规则文件, 片段清单或正文向量不可用
    一律归一为 `AgentConfigError`: 让入口直接失败, 而不是等运行到一半才把配置问题
    说成"制度里没有相关规定".
    """

    llm = load_llm_settings(env)

    try:
        policy = load_policy(rules_path)
    except PolicyLoadError as exc:
        raise AgentConfigError(f"规则文件不可用: {exc}") from exc

    client = OpenAI(
        api_key=llm.api_key,
        base_url=llm.base_url,
        timeout=REQUEST_TIMEOUT,
        max_retries=0,
    )
    policy_index = load_policy_index(
        client=client,
        model=app_settings.embedding_model,
        cache_path=app_settings.embedding_cache_path,
        chunk_list_path=chunk_list_path,
    )
    return ReviewRuntime(
        client=client,
        policy=policy,
        policy_index=policy_index,
        model_name=llm.model_name,
    )


def run_review_round(command: ReviewCommand, *, runtime: ReviewRuntime) -> AgentRunOutcome:
    """按 command 执行一轮审查, 返回现有 AgentRunOutcome.

    每轮新建会话并登记 command 里的补充原文 (轮次号由会话按登记顺序给出); 这里不写
    运行记录, 不保存材料, 也不做状态转换——那是调用方与本地记录层的职责.
    """

    session = ReviewSession.start(command.material)
    for text in command.supplements:
        session.record_supplement(text)
    return run_review(
        command.user_request,
        session=session,
        policy=runtime.policy,
        client=runtime.client,
        policy_index=runtime.policy_index,
        model_name=runtime.model_name,
        reference_date=command.reference_date,
    )


__all__ = [
    "ReviewCommand",
    "ReviewRuntime",
    "build_review_runtime",
    "run_review_round",
]
