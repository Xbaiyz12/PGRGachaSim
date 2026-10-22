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
from gsuid_core.utils.html_render import render_html_to_bytes

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
# 重复角色返还碎片数：S(6★)=30 / A(5★)=18 / B(4★)=6（泛用机体口径）
_DUP_SHARDS = {6: 30, 5: 18, 4: 6}
_BG_BASE64_CACHE: str = ""


class PoolSelectCard(TypedDict, total=False):
    name: str
    figure: str
    theme_no: int | str
    fate_no: int | str


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
        # 已拥有角色再抽到 → 转碎片展示（顶部标签 + 底部碎片小卡，立绘保持原亮度）
        # 覆盖 6/5/4★ 角色，对应 x30 / x18 / x6；同批次后续同角色也判为重复
        is_char = result["category"] == "character" and result["level"] >= 4
        duplicate = bool(is_char and result["name"] in owned)
        if is_char:
            owned.add(result["name"])
        # 重复返还碎片数：S(6★)=30 / A(5★)=18 / B(4★)=6
        shard_amount = _DUP_SHARDS.get(result["level"], 6) if duplicate else 0
        # 意识（带立绘的材料）随机分配 1-6 号位置：1&4 左 / 2&5 中 / 3&6 右
        position = random.randint(1, 6) if (result["category"] == "material" and figure_url) else 0
        items.append(
            {
                "name": result["name"],
                "icon": _icon_data_url(result["icon"]),
                "figure": _icon_data_url(figure_url) if figure_url else "",
                "amount": result["amount"],
                "level": result["level"],
                "is_up": result["is_up"],
                "category": result["category"],
                "duplicate": duplicate,
                "shard_amount": shard_amount,
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
    except httpx.HTTPError as error:
        logger.warning(f"[战双·模拟抽卡] 外置渲染异常: {error}")
        return None


async def _render_html_pytakumi(html: str) -> bytes | None:
    """pytakumi 无浏览器渲染，速度比 Playwright 快约 3-5 倍，内存占用低"""
    try:
        start = time.time()
        image = await render_html_to_bytes(
            html,
            max_width=2000,
            allow_refit=True,
            image_format="jpeg",
            jpeg_quality=90,
            root_max_width=2000,
        )
        logger.info(f"[战双·模拟抽卡] pytakumi 渲染成功，耗时 {time.time() - start:.2f}s")
        return image
    except (RuntimeError, OSError, ValueError) as error:
        logger.warning(f"[战双·模拟抽卡] pytakumi 渲染失败: {error}")
        return None


async def _render_html_local(
    template: Template,
    context: dict[str, object],
    force_playwright: bool = False,
) -> bytes | None:
    """本地渲染：pytakumi 优先，失败时回退 Playwright。

    pytakumi 对 display:flex/grid 容器有子元素渲染缺陷，调用方若模板
    依赖这两种布局（如 pool_select），应传 force_playwright=True。
    """
    html = template.render(**context)

    # 优先使用 pytakumi（无浏览器、低内存、快速）
    if not force_playwright:
        pytakumi_image = await _render_html_pytakumi(html)
        if pytakumi_image is not None:
            return pytakumi_image

    # 兜底 Playwright
    if _async_playwright is None:
        logger.warning("[战双·模拟抽卡] pytakumi 不可用且 Playwright 未安装，跳过图片渲染")
        return None
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

    template = gacha_templates.get_template("gacha_result.html")

    # 优先 pytakumi 并发渲染（无浏览器进程开销，速度优势明显）
    pytakumi_tasks = [_render_html_pytakumi(template.render(**ctx)) for ctx in batches]
    pytakumi_results = await asyncio.gather(*pytakumi_tasks)
    pytakumi_valid = [img for img in pytakumi_results if img is not None]
    if len(pytakumi_valid) == len(batches):
        return pytakumi_valid
    if pytakumi_valid:
        logger.warning(
            f"[战双·模拟抽卡] pytakumi 批量渲染部分失败 {len(pytakumi_valid)}/{len(batches)}，回退 Playwright"
        )

    # 兜底 Playwright：复用单浏览器进程
    if _async_playwright is None:
        logger.warning("[战双·模拟抽卡] pytakumi 失败且 Playwright 未安装")
        return pytakumi_valid
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
    """渲染卡池选择图：每版本一张卡，卡内逐行列出各 UP 角色及其主题/命运编号"""
    rendered: list[dict[str, object]] = []
    for card in cards:
        figure = card["figure"] if "figure" in card else ""
        name = card["name"] if "name" in card else ""
        roles = card["roles"] if "roles" in card else []
        rendered.append(
            {
                "name": name,
                "figure": _icon_data_url(figure) if figure else "",
                "roles": roles,
            }
        )
    context = {"cards": rendered, "prefix": prefix, "title": title}

    if GachaSimConfig.get_config("PGRGachaRemoteRenderEnable").data:
        html = gacha_templates.get_template("pool_select.html").render(**context)
        remote = await _render_remote(html)
        if remote is not None:
            return remote

    # 模板为 absolute 布局，pytakumi 可正常渲染
    return await _render_html_local(
        gacha_templates.get_template("pool_select.html"),
        context,
    )


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
