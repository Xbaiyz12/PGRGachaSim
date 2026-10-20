"""战双模拟抽卡 - 渲染抽卡结果"""

import time
import base64
import random
import asyncio
from typing import TypedDict
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
_BG_BASE64_CACHE: str = ""


class PoolSelectCard(TypedDict, total=False):
    name: str
    figure: str
    theme_no: int | str
    fate_no: int | str
    single_no: int | str


def _get_bg_base64() -> str:
    """获取背景图 base64，优先使用内存缓存避免重复 IO"""
    global _BG_BASE64_CACHE
    if not _BG_BASE64_CACHE:
        bg_path = TEMPLATE_DIR.parent / "texture2d" / "gacha_bg.jpg"
        if bg_path.exists():
            _BG_BASE64_CACHE = _image_to_base64(bg_path)
    return _BG_BASE64_CACHE


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


def _build_items(
    results: list[GachaResult],
    owned_names: set[str] | None = None,
) -> list[dict[str, object]]:
    owned = owned_names or set()
    items: list[dict[str, object]] = []
    for result in results:
        is_fragment = result["category"] == "fragment"
        figure_url = "" if is_fragment else result["figure"]
        # 已拥有角色再抽到 → 转碎片展示（立绘变暗 + 顶部标签 + 底部碎片小卡）
        duplicate = (
            bool(owned)
            and result["category"] == "character"
            and result["level"] >= 5
            and result["name"] in owned
        )
        # 意识（带立绘的材料）随机分配 1-6 号位置：1&4 左 / 2&5 中 / 3&6 右
        position = random.randint(1, 6) if (result["category"] == "material" and figure_url) else 0
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
                "duplicate": duplicate,
                "shard_icon": _icon_data_url(result.get("shards_icon", "")) if duplicate else "",
                "position": position,
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
    owned_names: set[str] | None = None,
) -> bytes | None:
    """渲染抽卡结果图片，渲染不可用时返回 None"""
    context = {
        "pool_name": pool_name,
        "draw_type": draw_type,
        "items": _build_items(results, owned_names),
        "signature_code": signature_code,
        "avatar": _image_to_base64(avatar_path) if avatar_path is not None else "",
        "daily_count": daily_count,
        "background": _get_bg_base64(),
    }

    if GachaSimConfig.get_config("PGRGachaRemoteRenderEnable").data:
        html = gacha_templates.get_template("gacha_result.html").render(**context)
        remote = await _render_remote(html)
        if remote is not None:
            return remote

    return await _render_html_local(gacha_templates.get_template("gacha_result.html"), context)


async def render_gacha_results_batch(
    batches: list[dict[str, object]],
) -> list[bytes]:
    """批量渲染抽卡结果（如百连），复用单个 Chromium 进程以大幅降低冷启动开销"""
    if not batches:
        return []

    # 若启用了远程渲染，则走并发远程请求
    if GachaSimConfig.get_config("PGRGachaRemoteRenderEnable").data:
        template = gacha_templates.get_template("gacha_result.html")
        tasks = []
        for ctx in batches:
            html = template.render(**ctx)
            tasks.append(_render_remote(html))
        remote_results = await asyncio.gather(*tasks)
        valid_results = [res for res in remote_results if res is not None]
        if len(valid_results) == len(batches):
            return valid_results

    if _async_playwright is None:
        logger.warning("[战双·模拟抽卡] Playwright 未安装，跳过图片渲染")
        return []

    template = gacha_templates.get_template("gacha_result.html")
    rendered_images: list[bytes] = []

    try:
        async with _async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": 2000, "height": 1100})
                for ctx in batches:
                    html = template.render(**ctx)
                    await page.set_content(html, wait_until="load")
                    container = page.locator(".container")
                    await container.wait_for(timeout=5000)
                    img_bytes = await container.screenshot(type="jpeg", quality=90)
                    rendered_images.append(img_bytes)
            finally:
                await browser.close()
    except TimeoutError as error:
        logger.error(f"[战双·模拟抽卡] 批量渲染等待超时: {error}")
    except (PlaywrightError, OSError) as error:
        logger.error(f"[战双·模拟抽卡] 批量渲染失败: {error}")

    return rendered_images


async def render_pool_select(cards: list[PoolSelectCard], prefix: str = "pgr", title: str = "切换卡池") -> bytes | None:
    """渲染卡池选择图：每格显示图片 + 名称 + 编号"""
    rendered: list[dict[str, object]] = []
    for card in cards:
        figure = card["figure"] if "figure" in card else ""
        name = card["name"] if "name" in card else ""
        theme_no = card["theme_no"] if "theme_no" in card else None
        fate_no = card["fate_no"] if "fate_no" in card else None
        single_no = card["single_no"] if "single_no" in card else None
        rendered.append(
            {
                "name": name,
                "figure": _icon_data_url(figure) if figure else "",
                "theme_no": theme_no,
                "fate_no": fate_no,
                "single_no": single_no,
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
