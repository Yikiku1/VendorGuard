"""工作台页面与静态资源: 单页壳, 由 FastAPI 直接提供.

M4 方案第 8 节. 这里只做两件事: 返回页面壳 (`GET /workbench`) 与返回固定的几个静态
资源 (`GET /assets/workbench/<name>`).

- 页面壳是**公开**的: 未登录时前端只显示登录区域; 数据接口仍然要求 Bearer Token.
- 静态资源用**白名单**: 只服务下面登记过的文件名, 不按用户输入拼路径, 也就不存在
  目录穿越; 资源不打 CDN, 全部跟随应用一起发布 (方案 8.3).
- JS 与 CSS 放在这个目录里, 与后端代码分开, 便于单独审视.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse

# 资源目录: 与后端代码同一个包, 随应用一起发布.
ASSET_DIR = Path(__file__).resolve().parent

# 白名单: 资源名 → 内容类型. 只有这里的名字能被取到.
_ASSETS: dict[str, str] = {
    "styles.css": "text/css",
    "app.js": "text/javascript",
}

# 演示期会反复改页面资源: 让浏览器每次回来校验 (带 ETag, 未改就 304), 免得改了样式
# 刷新还是旧样子.
_NO_CACHE = {"Cache-Control": "no-cache"}

router = APIRouter(tags=["工作台"])


@router.get("/workbench", response_class=FileResponse, include_in_schema=False)
def workbench_page() -> FileResponse:
    """返回工作台页面壳 (公开): 登录区域与主区域都在同一个 HTML 里."""

    return FileResponse(ASSET_DIR / "index.html", media_type="text/html", headers=_NO_CACHE)


@router.get("/assets/workbench/{asset_name}", response_class=FileResponse, include_in_schema=False)
def workbench_asset(asset_name: str) -> FileResponse:
    """返回登记过的静态资源; 未登记的名字一律 404."""

    if asset_name not in _ASSETS or PurePosixPath(asset_name).name != asset_name:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="资源不存在")
    return FileResponse(ASSET_DIR / asset_name, media_type=_ASSETS[asset_name], headers=_NO_CACHE)


__all__ = ["ASSET_DIR", "router"]
