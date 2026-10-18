"""战双模拟抽卡 - 渲染抽卡结果"""

import time
import base64
from pathlib import Path

import httpx
from jinja2 import Template, Environment, FileSystemLoader

from gsuid_core.logger import logger

from .gacha_config import GachaSimConfig
from .pool_manager import pool_manager
from .gacha_service import GachaResult

TEMPLATE_DIR = Path(__file__).parent / "templates"
gacha_templates = Environment(loader=FileSystemLoader([str(TEMPLATE_DIR)]))

try:
    from playwright.async_api import Error as PlaywrightError, async_playwright as _async_playwright
except ImportError:
    _async_playwright = None

    class PlaywrightError(Exception):
        pass


_LEVEL_LABELS = {6: "红", 5: "橙", 4: "紫", 3: "蓝"}


def _image_to_base64(path: Path) -> str:
    """本地图片转 data URL，空文件返回空"""
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if not raw:
        return ""
    suffix = path.suffix.lower().lstrip(".")
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(suffix, "png")
    return f"data:image/{mime};base64,{base64.b64encode(raw).decode()}"


def _icon_data_url(url: str) -> str:
    """图标只读本地缓存，缺失时返回空（不回退远程，避免请求 wiki）"""
    if not url:
        return ""
    path = pool_manager.find_icon_path(url)
    if path is None:
        return ""
    return _image_to_base64(path)


def _build_items(results: list[GachaResult]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for result in results:
        is_fragment = result["category"] == "fragment"
        figure_url = "" if is_fragment else result["figure"]
        items.append(
            {
                "name": result["name"],
                "icon": _icon_data_url(result["icon"]),
                "figure": _icon_data_url(figure_url) if figure_url else "",
                "amount": result["amount"],
                "level": result["level"],
                "pillar_level": result["pillar_level"],
                "is_up": result["is_up"],
                "category": result["category"],
            }
        )
    return items


async def _render_remote(html: str) -> bytes | None:
    url = str(GachaSimConfig.get_config("PGRGachaRemoteRenderUrl").data)
    if not url:
        return None
    try:
        start = time.time()
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, json={"html": html})
        if response.status_code != 200:
            logger.warning(f"[战双·模拟抽卡] 外置渲染失败: HTTP {response.status_code}")
            return None
        logger.info(f"[战双·模拟抽卡] 外置渲染成功，耗时 {time.time() - start:.2f}s")
        return response.content
    except (httpx.HTTPError, httpx.TimeoutException) as error:
        logger.warning(f"[战双·模拟抽卡] 外置渲染异常: {error}")
        return None


async def _render_html_local(template: Template, context: dict[str, object]) -> bytes | None:
    """本地 Playwright 渲染，视口与画布对齐并安全释放浏览器"""
    if _async_playwright is None:
        logger.warning("[战双·模拟抽卡] Playwright 未安装，跳过图片渲染")
        return None
    html = template.render(**context)
    try:
        async with _async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": 2000, "height": 1100})
                await page.set_content(html, wait_until="load")
                container = page.locator(".container")
                await container.wait_for(timeout=5000)
                return await container.screenshot(type="jpeg", quality=90)
            finally:
                await browser.close()
    except TimeoutError as error:
        logger.error(f"[战双·模拟抽卡] 本地渲染元素等待超时: {error}")
        return None
    except (PlaywrightError, OSError) as error:
        logger.error(f"[战双·模拟抽卡] 本地渲染失败: {error}")
        return None


async def render_gacha_result(
    results: list[GachaResult],
    pool_name: str,
    signature_code: str = "",
    draw_type: int = 10,
    avatar_path: Path | None = None,
    daily_count: int = 0,
) -> bytes | None:
    """渲染抽卡结果图片，渲染不可用时返回 None"""
    context = {
        "pool_name": pool_name,
        "draw_type": draw_type,
        "items": _build_items(results),
        "signature_code": signature_code,
        "avatar": _image_to_base64(avatar_path) if avatar_path is not None else "",
        "daily_count": daily_count,
        "background": _image_to_base64(TEMPLATE_DIR.parent / "texture2d" / "gacha_bg.jpg")
        if (TEMPLATE_DIR.parent / "texture2d" / "gacha_bg.jpg").exists()
        else "",
    }

    if GachaSimConfig.get_config("PGRGachaRemoteRenderEnable").data:
        html = gacha_templates.get_template("gacha_result.html").render(**context)
        remote = await _render_remote(html)
        if remote is not None:
            return remote

    return await _render_html_local(gacha_templates.get_template("gacha_result.html"), context)


async def render_pool_select(
    cards: list[dict[str, object]], prefix: str = "pgr", title: str = "切换卡池"
) -> bytes | None:
    """渲染卡池选择图：每格显示图片 + 名称 + 编号"""
    rendered: list[dict[str, object]] = []
    for card in cards:
        figure = card.get("figure")
        rendered.append(
            {
                "name": card.get("name", ""),
                "figure": _icon_data_url(figure) if isinstance(figure, str) else "",
                "theme_no": card.get("theme_no"),
                "fate_no": card.get("fate_no"),
                "single_no": card.get("single_no"),
            }
        )
    context = {"cards": rendered, "prefix": prefix, "title": title}

    if GachaSimConfig.get_config("PGRGachaRemoteRenderEnable").data:
        html = gacha_templates.get_template("pool_select.html").render(**context)
        remote = await _render_remote(html)
        if remote is not None:
            return remote

    return await _render_html_local(gacha_templates.get_template("pool_select.html"), context)


def format_text_result(
    results: list[GachaResult],
    pool_name: str,
    signature_code: str = "",
) -> str:
    """渲染失败时的纯文本结果"""
    lines = [f"【{pool_name}】"]
    for result in results:
        up_tag = " [UP]" if result["is_up"] else ""
        level_tag = _LEVEL_LABELS.get(result["level"], "")
        lines.append(f"[{level_tag}] {result['name']}{up_tag}")
    top = [result for result in results if result["level"] >= 5]
    if top:
        lines.append("")
        for result in top:
            lines.append(f"✨ {result['name']} - 第{result['pity_count']}抽")
    if signature_code:
        lines.append(f"\n特征码: {signature_code}")
    return "\n".join(lines)
