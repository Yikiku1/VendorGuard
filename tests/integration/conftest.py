"""集成测试的公共夹具.

M4-3 起应用启动会装载审查依赖 (模型配置 + 制度片段清单与向量缓存), 那些是**真实运行**
才需要的东西. 集成测试默认把这一处装载换成替身, 并把本地审查记录目录指向临时目录:
这样既有用例 (健康检查, 登录, 案件流程) 不必依赖模型配置或检索缓存, 也不会往工作区的
`var/reviews/` 写东西. 需要真实审查链路的用例, 自行用 `app.dependency_overrides`
把 runtime 与 store 换成自己准备好的替身.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch

import vendorguard.app as app_module


class StubReviewRuntime:
    """审查依赖替身: 只表示"启动时装载过", 接口用例不会真的用它跑一轮."""

    client: Any = None
    policy: Any = None
    policy_index: Any = None
    model_name: str = "stub-model"


@pytest.fixture(autouse=True)
def stubbed_review_state(monkeypatch: MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """把启动时的审查依赖装载换成替身, 记录目录指向本次测试的临时目录."""

    monkeypatch.setattr(
        app_module,
        "build_review_runtime",
        lambda *args, **kwargs: StubReviewRuntime(),
    )
    monkeypatch.setenv("VENDORGUARD_REVIEW_DATA_DIR", str(tmp_path / "reviews"))
    yield
