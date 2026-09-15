"""CLI 入口的两轮闭环测试: 补充之后第二轮是独立的一次运行.

M4-1 之后 `main()` 只做三件事: 读材料, 调审查应用层装载依赖, 按轮次提交命令; 模型
客户端与检索数据的接线都在 `review_application`, 所以替身注入点跟着移过去. 这里验证
命令行这一层的编排:

- 第一轮追问后, 命令行把用户补充追加进命令, 由应用层新建会话重新读取, 校验与检索;
- 每轮都经**现有 `run_review()`** 执行一次, 两轮共用同一个 runtime 客户端;
- 第二轮不携带上一轮的检索结果或校验结论, 开场消息只带补充原文与 `user_supplement@round:N`;
- 最终按 `submit_report` 的报告收尾, 退出码为 0。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pytest import MonkeyPatch

from vendorguard import agent, review_application
from vendorguard.agent import PolicyIndex, main
from vendorguard.config import Settings
from vendorguard.retrieval.embedding import EMBEDDING_DIMENSION, ChunkVectors

# 复用既有的替身与剧本, 避免出现第二套假数据
from .test_agent import (
    MATERIALS_DIR,
    FakeClient,
    FakeEmbeddings,
    ask_user_response,
    check_arguments,
    chunk_record,
    read_response,
    report_response,
    search_response,
    tool_call_response,
)

SUPPLEMENT = "补充说明: 营业执照有效期至 2030年01月31日"


def fake_policy_index() -> PolicyIndex:
    """造一份最小检索数据: 两个现行节点, 查询向量与第一条同向."""

    records = tuple(chunk_record(index) for index in (1, 2))
    first = (1.0, *([0.0] * (EMBEDDING_DIMENSION - 1)))
    second = (0.0, 1.0, *([0.0] * (EMBEDDING_DIMENSION - 2)))
    return PolicyIndex(
        records=records,
        vectors=ChunkVectors(
            model="qwen3.7-text-embedding",
            cache_key="c" * 64,
            dimension=EMBEDDING_DIMENSION,
            vectors=(first, second),
        ),
        model="qwen3.7-text-embedding",
    )


@pytest.fixture
def cli_env(monkeypatch: MonkeyPatch, tmp_path: Path) -> dict:
    """把 main() 的外部依赖换成替身: 模型客户端, 检索数据, 运行记录与用户输入."""

    index = fake_policy_index()
    client = FakeClient(
        [
            # 第一轮: 读到材料但缺日期, 追问
            read_response(),
            tool_call_response(check_arguments(valid_until=None), call_id="call_check_1"),
            ask_user_response("请提供营业执照有效期截止日?", call_id="call_ask_1"),
            # 第二轮: 重新读取 -> 重新校验(引用补充来源) -> 重新检索 -> 交报告
            read_response(call_id="call_read_2"),
            tool_call_response(
                check_arguments(valid_until="2030-01-31", date_source="user_supplement@round:1"),
                call_id="call_check_2",
            ),
            search_response(call_id="call_search_2"),
            report_response(
                call_id="call_report_2",
                material_sources=("user_supplement@round:1", "license_complete@page:1"),
                policy_citations=(index.records[0].node_key,),
            ),
        ],
        embeddings=FakeEmbeddings(),
    )

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    written: list[Path] = []

    # 命令行用例绝不允许连真实模型端点: 万一某条路径漏了替身去自己建客户端,
    # 它也只会打到本机死端口而失败, 不会消耗真实调用.
    monkeypatch.setenv("VENDORGUARD_LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("VENDORGUARD_LLM_MODEL", "dead-endpoint-model")
    monkeypatch.setenv("VENDORGUARD_LLM_API_KEY", "sk-dead-endpoint")

    def record(outcome, *, model_name, log_dir, started_at, simulated=True):
        # 忽略 main() 传入的 logs/agent-runs, 把记录写进临时目录: 测试不碰工作区
        path = runs_dir / "run.json"
        path.write_text(json.dumps({"kind": outcome.kind}, ensure_ascii=False), encoding="utf-8")
        written.append(path)
        return path

    # 模型客户端与检索数据由应用层装载, 注入点跟着移到那里; 运行记录仍由 CLI 写
    monkeypatch.setattr(review_application, "OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(
        review_application,
        "load_llm_settings",
        lambda env: SimpleNamespace(
            model_name="fake-model", api_key="sk-test", base_url="https://example.test/v1"
        ),
    )
    monkeypatch.setattr(
        agent,
        "load_settings",
        lambda **kwargs: Settings(
            _env_file=None,
            embedding_model="qwen3.7-text-embedding",
            embedding_cache_path=tmp_path / "cache.json",
        ),
    )
    monkeypatch.setattr(review_application, "load_policy_index", lambda **kwargs: index)
    monkeypatch.setattr(agent, "write_run_log", record)
    monkeypatch.setattr("builtins.input", lambda prompt="": SUPPLEMENT)

    return {"client": client, "index": index, "written": written}


def test_cli_runs_two_rounds_and_ends_with_a_report(cli_env, capsys) -> None:
    """命令行两轮闭环: 追问 -> 登记补充 -> 第二轮重新校验与检索 -> 报告收尾."""

    exit_code = main(
        [
            str(MATERIALS_DIR / "license_complete.pdf"),
            "请检查这家供应商的材料",
            "--reference-date",
            "2026-09-01",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "[已登记第 1 轮用户补充]" in output
    assert "[检查结论]" in output
    assert "已交付结构化报告" in output
    assert cli_env["index"].records[0].node_key in output

    client = cli_env["client"]
    # 两轮各发三次请求: 读, 校验, 然后 (追问 / 检索+报告)
    assert len(client.completions.calls) == 7
    second_round_opening = client.completions.calls[3]["messages"]
    assert second_round_opening[0]["role"] == "system"
    opening_text = second_round_opening[1]["content"]
    assert "user_supplement@round:1" in opening_text
    assert "2030年01月31日" in opening_text
    # 第二轮是全新的一次运行: 开场里不带上一轮的检索结果或校验结论
    assert "node_key" not in opening_text
    assert "not_hit" not in opening_text
    assert not any(message["role"] == "tool" for message in second_round_opening[:2])


def test_cli_runs_each_round_through_the_existing_loop(cli_env, monkeypatch: MonkeyPatch) -> None:
    """每轮都经现有 run_review() 执行一次: CLI 没有第二套循环.

    应用层每轮新建会话, 所以两轮的 session 不是同一个对象 (上一轮登记的节点不会
    顺延), 但共用同一个 runtime 客户端与检索数据; 补充原文随第二次命令进入新会话.
    """

    calls: list[dict] = []
    original = review_application.run_review

    def spy(*args, **kwargs):
        calls.append({"args": args, **kwargs})
        return original(*args, **kwargs)

    monkeypatch.setattr(review_application, "run_review", spy)

    assert (
        main(
            [
                str(MATERIALS_DIR / "license_complete.pdf"),
                "请检查这家材料的准入条件",
                "--reference-date",
                "2026-09-01",
            ]
        )
        == 0
    )

    assert len(calls) == 2
    first, second = calls
    # 每轮一个新会话: 补充原文靠新命令进入第二轮, 而不是把上一轮的结论塞给它
    assert first["session"] is not second["session"]
    assert first["session"].supplement_rounds == 0
    assert second["session"].supplement_rounds == 1
    assert second["session"].supplements == [SUPPLEMENT]
    # 两轮共用同一个 runtime 客户端与检索数据, 用户要求原样传递
    assert second["client"] is cli_env["client"]
    assert first["client"] is second["client"]
    assert second["policy_index"] is cli_env["index"]
    assert first["args"][0] == "请检查这家材料的准入条件"


def test_cli_without_supplement_keeps_the_question_exit(cli_env, capsys, monkeypatch) -> None:
    """用户放弃补充时: 不跑第二轮, 以追问收尾并返回 0."""

    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    before = len(cli_env["client"].completions.calls)

    exit_code = main(
        [
            str(MATERIALS_DIR / "license_complete.pdf"),
            "请检查这家供应商的材料",
        ]
    )

    assert exit_code == 0
    assert "[追问]" in capsys.readouterr().out
    # 只发了第一轮的三次请求
    assert len(cli_env["client"].completions.calls) - before == 3


def test_cli_does_not_load_review_dependencies_itself(cli_env, monkeypatch: MonkeyPatch) -> None:
    """装载模型与检索数据的接线只在应用层: CLI 不再自己建客户端或读片段清单."""

    def forbidden(*args, **kwargs):
        raise AssertionError("CLI 不应自己装载模型客户端或检索数据, 这条接线在应用层")

    monkeypatch.setattr(agent, "OpenAI", forbidden)
    monkeypatch.setattr(agent, "load_policy_index", forbidden)

    assert main([str(MATERIALS_DIR / "license_complete.pdf"), "请检查这家供应商的材料"]) == 0


def test_cli_rejects_scanned_pdf_before_loading_review_dependencies(
    cli_env, capsys, monkeypatch: MonkeyPatch
) -> None:
    """扫描件在材料层就被拒: 退出码 2, 不发模型请求, 也不去装载审查依赖."""

    loaded: list[dict] = []

    def forbidden(**kwargs):
        loaded.append(kwargs)
        raise AssertionError("材料不可读时不应装载审查依赖")

    monkeypatch.setattr(review_application, "build_review_runtime", forbidden)

    exit_code = main([str(MATERIALS_DIR / "license_scanned.pdf")])

    assert exit_code == 2
    assert loaded == []
    assert "启动失败" in capsys.readouterr().out
    assert cli_env["client"].completions.calls == []
