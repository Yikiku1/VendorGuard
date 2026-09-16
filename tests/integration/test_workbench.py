"""工作台页面与静态资源: 公开页面壳, 自包含资源, 前端不自己推导状态.

M4 方案第 8 节. 页面壳 (`GET /workbench`) 不需要 Token: 未登录时只显示登录区域, 数据
接口仍然要求 Bearer Token. 静态资源只有固定几个文件 (白名单), 资源不走 CDN, 页面上的
动态内容必须用 `textContent` 渲染——这几条都能在 HTTP 层钉住, 不需要浏览器.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vendorguard.app import create_app
from vendorguard.workbench import ASSET_DIR

# 样式约束: 平面风格, 不用渐变与投影 (2026-09-16 用户明确要求 linear 风).
CSS = (ASSET_DIR / "styles.css").read_text(encoding="utf-8")
APP_JS = (ASSET_DIR / "app.js").read_text(encoding="utf-8")


def test_workbench_page_is_public_and_self_contained() -> None:
    """页面壳公开可访问, 只引用本站资源, 关键区域都有锚点."""

    with TestClient(create_app()) as client:
        response = client.get("/workbench")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    for anchor in (
        'id="login-form"',
        'id="history-list"',
        'id="review-form"',
        'id="material-file"',
        'id="reference-date"',
        'id="status-line"',
        'id="report-area"',
        'id="error-line"',
    ):
        assert anchor in body
    assert "/assets/workbench/styles.css" in body
    assert "/assets/workbench/app.js" in body
    # 不依赖 CDN: 页面里没有任何外部地址
    assert "http://" not in body
    assert "https://" not in body


@pytest.mark.parametrize(
    ("asset", "expected_type"),
    [("styles.css", "css"), ("app.js", "javascript")],
)
def test_workbench_assets_are_served(asset: str, expected_type: str) -> None:
    """两个静态资源都能取到, 内容类型对得上."""

    with TestClient(create_app()) as client:
        response = client.get(f"/assets/workbench/{asset}")

    assert response.status_code == 200
    assert expected_type in response.headers["content-type"]
    assert len(response.text) > 200


@pytest.mark.parametrize(
    "path",
    [
        "/assets/workbench/unknown.js",
        "/assets/workbench/../app.py",
        "/assets/workbench/%2e%2e%2fapp.py",
        "/assets/workbench/app.js/extra",
    ],
)
def test_workbench_assets_are_a_fixed_whitelist(path: str) -> None:
    """静态资源只服务登记过的文件名: 其它路径一律 404, 不按用户输入读磁盘."""

    with TestClient(create_app()) as client:
        response = client.get(path)

    assert response.status_code == 404


def test_workbench_page_does_not_require_authentication() -> None:
    """页面壳是公开的, 但数据接口仍然要 Token (401 由前端清 Token 回登录)."""

    with TestClient(create_app()) as client:
        page = client.get("/workbench")
        api = client.get("/api/reviews")

    assert page.status_code == 200
    assert api.status_code == 401


def test_workbench_frontend_renders_user_input_as_text() -> None:
    """前端不拼 innerHTML, Token 只放 sessionStorage, 动作读服务端的 allowed_actions."""

    # 用带点的写法判"真的没在赋值": 注释里讲这条规则时提到这个词不算
    assert ".innerHTML" not in APP_JS
    assert "textContent" in APP_JS
    assert "sessionStorage" in APP_JS
    assert "allowed_actions" in APP_JS
    # 不依赖 CDN, 也不做实时推送 (方案 4.2: 只等一次响应)
    assert "https://" not in APP_JS
    assert "EventSource" not in APP_JS
    assert "WebSocket" not in APP_JS


def test_workbench_stylesheet_is_flat() -> None:
    """样式是平面风格: 没有渐变与投影, 圆角全部走同一个 4px 变量 (linear 风)."""

    assert "gradient" not in CSS
    assert "box-shadow" not in CSS
    assert "--radius: 4px" in CSS

    values = {
        line.split("border-radius:")[1].split(";")[0].strip()
        for line in CSS.splitlines()
        if "border-radius:" in line
    }
    assert values == {"var(--radius)"}


def test_workbench_frontend_has_no_hard_coded_records() -> None:
    """前端不写死任何示例记录或材料标识: 页面内容只能来自接口返回的数据."""

    for forbidden in ("license_complete", "material@page", "11111111-1111-4111-8111"):
        assert forbidden not in APP_JS
    # 接口基址只写一次, 其余地址都由它拼出来
    assert APP_JS.count('"/api/reviews"') == 1


def test_workbench_asset_dir_holds_the_pages() -> None:
    """资源目录里只有这三个文件: 页面资源与后端代码分开, 便于审视."""

    names = sorted(item.name for item in Path(ASSET_DIR).iterdir() if item.is_file())
    assert names == ["__init__.py", "app.js", "index.html", "styles.css"]


# ---------------------------------------------------------------------------
# M4-6: 轨迹, 来源, 引用展开与三个动作
# ---------------------------------------------------------------------------


def test_workbench_page_exposes_the_interaction_anchors() -> None:
    """页面壳里有轨迹, 动作区与三个动作的锚点 (交互全部由前端按 allowed_actions 控制)."""

    with TestClient(create_app()) as client:
        body = client.get("/workbench").text

    for anchor in (
        'id="trace-list"',
        'id="action-area"',
        'id="supplement-form"',
        'id="supplement-text"',
        'id="feedback-form"',
        'id="feedback-comment"',
        'id="feedback-confirm"',
        'id="feedback-recheck"',
        'id="rerun-button"',
        'id="feedback-note"',
    ):
        assert anchor in body
    # 三个动作的表单默认隐藏, 由 allowed_actions 决定显示哪一个
    assert "hidden" in body


def test_workbench_frontend_calls_only_the_review_api() -> None:
    """前端只调审查接口与认证接口: 查看记录时不会再去检索或跑一轮."""

    for path in ("/auth/login", "/auth/me", '"/api/reviews"'):
        assert path in APP_JS
    # 代码里出现的 /api/ 路径只有审查接口这一个 (前端没有别的后端入口)
    assert set(re.findall(r'"/api/[^"]*"', APP_JS)) == {'"/api/reviews"'}
    # 三个动作各自的接口 (补充 / 反馈 / 重跑)
    for suffix in ("/supplements", "/feedback", "/reruns"):
        assert suffix in APP_JS


def test_workbench_frontend_shows_saved_evidence_instead_of_re_searching() -> None:
    """制度引用展开用本轮保存的节点原文: 来源与轨迹都从轮次里读, 不重新请求."""

    # 轨迹按事件顺序渲染, 并且读的是记录里的 tool_events
    assert "tool_events" in APP_JS
    assert "renderTrace" in APP_JS
    # 条款原文从本轮 search_policy 事件保存的 nodes 里取 (方案 4.5)
    assert "detail.nodes" in APP_JS or "detail && event.detail.nodes" in APP_JS
    assert "edition_key" in APP_JS
    # 材料来源靠 PDF Blob 与页码定位
    assert "createObjectURL" in APP_JS
    assert "#page=" in APP_JS
    # 用户补充来源直接显示原文 (从记录的 supplements 里取)
    assert "supplements" in APP_JS
    assert "user_supplement" in APP_JS


def test_workbench_frontend_collapses_details_and_expands_failures() -> None:
    """轨迹与引用用原生 details: 默认折叠, 出错的那一步自动展开, 键盘可用."""

    assert "createElement" in APP_JS
    assert '"details"' in APP_JS
    assert '"summary"' in APP_JS
    assert 'setAttribute("open"' in APP_JS
    # 报告里保留"初审报告不是准入决定"的固定边界提示
    assert "初审报告不是准入决定" in APP_JS


# ---------------------------------------------------------------------------
# 亮色模式
# ---------------------------------------------------------------------------


def test_workbench_supports_a_light_theme() -> None:
    """亮色配色存在且跟随系统: 手动选择与 prefers-color-scheme 两条入口都有值."""

    assert ':root[data-theme="light"]' in CSS
    assert "prefers-color-scheme: light" in CSS
    assert "color-scheme: light" in CSS
    # 亮色下同样是平面: 不加渐变与投影 (整份样式的平面测试仍然生效)
    assert "gradient" not in CSS
    assert "box-shadow" not in CSS
    # 两套配色共用同一个圆角变量, 不引入第二种圆角
    assert "--radius: 4px" in CSS


def test_workbench_theme_toggle_is_in_the_page() -> None:
    """页面壳里有主题切换按钮, 并且它不在登录后才出现的区域里."""

    with TestClient(create_app()) as client:
        body = client.get("/workbench").text

    assert 'id="theme-toggle"' in body
    # 切换按钮在顶栏 (登录前也能看到), 不在 app-view 里
    topbar = body.split("<main", 1)[0]
    assert 'id="theme-toggle"' in topbar


def test_workbench_theme_choice_is_remembered_per_browser() -> None:
    """手动选择记在 localStorage, 没选择时跟随系统; 主题写在 html 的 data-theme 上."""

    assert "localStorage" in APP_JS
    assert "prefers-color-scheme" in APP_JS
    assert "dataset.theme" in APP_JS
    assert "matchMedia" in APP_JS
    # 只识别两种主题名
    assert '"light"' in APP_JS and '"dark"' in APP_JS
