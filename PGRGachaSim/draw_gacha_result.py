"""战双模拟抽卡 - 渲染抽卡结果"""

import time
import base64
import asyncio
from typing import TypedDict
from pathlib import Path

import httpx
from jinja2 import Template, Environment, FileSystemLoader

from gsuid_core.logger import logger
from gsuid_core.utils.html_render import render_html_to_bytes

from .gacha_config import GachaSimConfig
from .pool_manager import pool_manager, register_icon_reload_hook
from .gacha_service import GachaResult

TEMPLATE_DIR = Path(__file__).parent / "templates"
# 模板在运行时不会变，关掉 auto_reload 免得每次渲染都 stat 一次 3MB 模板
gacha_templates = Environment(loader=FileSystemLoader([str(TEMPLATE_DIR)]), auto_reload=False)

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
# 同一次抽卡里同一图标会被反复读取（意识/材料成堆出现），缓存后避免重复 IO + base64
_ICON_BASE64_CACHE: dict[str, str] = {}
_ICON_BASE64_CACHE_MAX = 512
# 百连批量渲染的并发上限：pytakumi 同时开太多会把内存打满
_BATCH_RENDER_CONCURRENCY = 4
# 外置渲染失败后的熔断窗口：默认地址常常没有服务，别让每次十连都等满 60s
_REMOTE_COOLDOWN_S = 120.0
_REMOTE_COOLDOWN_UNTIL = 0.0


def _clear_icon_base64_cache() -> None:
    _ICON_BASE64_CACHE.clear()


register_icon_reload_hook(_clear_icon_base64_cache)


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
    cached = _ICON_BASE64_CACHE.get(url)
    if cached is not None:
        # 命中后前移，实现 LRU：dict 保持插入序，重插即最新
        _ICON_BASE64_CACHE.pop(url)
        _ICON_BASE64_CACHE[url] = cached
        return cached
    path = pool_manager.find_icon_path(url)
    if path is None:
        # 缺图不写缓存：补图后无需重启即可生效（reload 也会清空缓存）
        return ""
    data_url = _image_to_base64(path)
    # 满了只淘汰最旧一条：整表 clear 会让同批其它图标反复重读磁盘
    while len(_ICON_BASE64_CACHE) >= _ICON_BASE64_CACHE_MAX:
        _ICON_BASE64_CACHE.pop(next(iter(_ICON_BASE64_CACHE)))
    _ICON_BASE64_CACHE[url] = data_url
    return data_url


def _build_items(
    results: list[GachaResult],
    owned_names: set[str] | None = None,
) -> list[dict[str, object]]:
    # 必须拷贝：入参 set 会在下面被 add 污染，百连多张图共用同一份会前后结果不一致
    owned = set(owned_names) if owned_names is not None else set()
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
        shard_amount = _DUP_SHARDS[result["level"]] if duplicate and result["level"] in _DUP_SHARDS else 0
        # 意识（带立绘的材料）的 1-6 号位置在抽卡服务里随机，保证同一次抽卡可复现
        position = result["position"]
        items.append(
            {
                "name": result["name"],
                "icon": _icon_data_url(result["icon"]),
                "figure": _icon_data_url(figure_url) if figure_url else "",
                "amount": result["amount"],
                "level": result["level"],
                "is_up": result["is_up"],
                "pity_count": result["pity_count"],
                "category": result["category"],
                "duplicate": duplicate,
                "shard_amount": shard_amount,
                "shard_icon": _icon_data_url(result["shards_icon"]) if duplicate else "",
                "position": position,
            }
        )
    return items


async def _render_remote(html: str) -> bytes | None:
    url = str(GachaSimConfig.get_config("PGRGachaRemoteRenderUrl").data)
    if not url or time.monotonic() < _REMOTE_COOLDOWN_UNTIL:
        return None
    try:
        start = time.time()
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(url, json={"html": html})
        if response.status_code != 200:
            logger.warning(f"[战双·模拟抽卡] 外置渲染失败: HTTP {response.status_code}")
            _trip_remote_cooldown()
            return None
        # 外置服务可能 200 返回一段 HTML 错误页，必须确认是图片再交给适配器
        content_type = response.headers.get("content-type", "").lower()
        if not content_type.startswith("image/") or not response.content:
            logger.warning(f"[战双·模拟抽卡] 外置渲染返回非图片内容: {content_type or '未知'}")
            _trip_remote_cooldown()
            return None
        logger.info(f"[战双·模拟抽卡] 外置渲染成功，耗时 {time.time() - start:.2f}s")
        return response.content
    except (httpx.HTTPError, ValueError) as error:
        # ValueError 覆盖 InvalidURL；失败后熔断一段时间，别让每次十连都等满超时
        logger.warning(f"[战双·模拟抽卡] 外置渲染异常: {error}")
        _trip_remote_cooldown()
        return None


def _trip_remote_cooldown() -> None:
    global _REMOTE_COOLDOWN_UNTIL
    _REMOTE_COOLDOWN_UNTIL = time.monotonic() + _REMOTE_COOLDOWN_S


async def _remote_reachable() -> bool:
    """批量渲染前先探一次：地址没起服务时别让每张图都白等一次连接超时"""
    if time.monotonic() < _REMOTE_COOLDOWN_UNTIL:
        return False
    url = str(GachaSimConfig.get_config("PGRGachaRemoteRenderUrl").data)
    if not url:
        return False
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.head(url)
    except (httpx.HTTPError, ValueError):
        _trip_remote_cooldown()
        return False
    # 4xx/5xx 说明对端不是可用的渲染服务，后续 POST 也只会白跑
    if response.status_code >= 400:
        _trip_remote_cooldown()
        return False
    return True


async def _render_html_pytakumi(html: str) -> bytes | None:
    """pytakumi 无浏览器渲染，速度比 Playwright 快约 3-5 倍，内存占用低"""
    start = time.time()
    try:
        image = await render_html_to_bytes(
            html,
            max_width=2000,
            allow_refit=True,
            image_format="jpeg",
            jpeg_quality=90,
            root_max_width=2000,
        )
    except (RuntimeError, OSError, ValueError) as error:
        logger.warning(f"[战双·模拟抽卡] pytakumi 渲染失败: {error}")
        return None
    # 空 bytes 说明渲染实际没出图，交给上层回退 Playwright
    if not image:
        logger.warning("[战双·模拟抽卡] pytakumi 返回空图片，回退 Playwright")
        return None
    logger.info(f"[战双·模拟抽卡] pytakumi 渲染成功，耗时 {time.time() - start:.2f}s")
    return image


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
    up_count: int = 0,
    since_up: int = 0,
) -> bytes | None:
    """渲染抽卡结果图片，渲染不可用时返回 None"""
    context = {
        "pool_name": pool_name,
        "draw_type": draw_type,
        "items": _build_items(results, owned_names),
        "signature_code": signature_code,
        "avatar": _image_to_base64(avatar_path) if avatar_path is not None else "",
        "daily_count": daily_count,
        "up_count": up_count,
        "since_up": since_up,
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
) -> list[bytes | None]:
    """批量渲染抽卡结果（百连合并转发用）。

    返回与 batches 等长的列表，失败项为 None；调用方据长度判断能否整批发合并转发。
    """
    if not batches:
        return []

    template = gacha_templates.get_template("gacha_result.html")
    html_list = [template.render(**ctx) for ctx in batches]
    images: list[bytes | None] = [None] * len(html_list)

    remote_enabled = GachaSimConfig.get_config("PGRGachaRemoteRenderEnable").data
    if remote_enabled and await _remote_reachable():
        remote_results = await asyncio.gather(*[_render_remote(html) for html in html_list])
        for index, result in enumerate(remote_results):
            images[index] = result

    # 远程未覆盖的再走 pytakumi，并发生效上限防止百连把内存打满
    semaphore = asyncio.Semaphore(_BATCH_RENDER_CONCURRENCY)

    async def _pytakumi_at(index: int) -> None:
        async with semaphore:
            images[index] = await _render_html_pytakumi(html_list[index])

    pending = [index for index, image in enumerate(images) if image is None]
    if pending:
        await asyncio.gather(*[_pytakumi_at(index) for index in pending])

    missing = [index for index, image in enumerate(images) if image is None]
    if not missing:
        return images

    if _async_playwright is None:
        logger.warning(f"[战双·模拟抽卡] {len(missing)} 张渲染失败且 Playwright 未安装")
        return images

    # 兜底 Playwright：复用单浏览器进程，逐张补齐；单张失败不牵连其它张
    logger.warning(f"[战双·模拟抽卡] 批量渲染 {len(missing)} 张回退 Playwright")
    try:
        async with _async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": 2000, "height": 1100})
                for index in missing:
                    try:
                        await page.set_content(html_list[index], wait_until="load")
                        container = page.locator(".container")
                        await container.wait_for(timeout=5000)
                        images[index] = await container.screenshot(type="jpeg", quality=90)
                    except (TimeoutError, PlaywrightError, OSError) as error:
                        logger.warning(f"[战双·模拟抽卡] 第 {index + 1} 张渲染失败: {error}")
            finally:
                await browser.close()
    except (TimeoutError, PlaywrightError, OSError) as error:
        logger.error(f"[战双·模拟抽卡] 批量渲染回退失败: {error}")

    return images


async def render_bailian_summary(
    items: list[dict[str, object]],
    pool_name: str,
    signature_code: str = "",
    daily_count: int = 0,
    up_count: int = 0,
    since_up: int = 0,
) -> bytes | None:
    """百连汇总图：单张图展示 100 抽概览与 5★ 及以上明细，避免多图刷屏"""
    counts = {"6": 0, "5": 0, "4": 0, "3": 0}
    for item in items:
        level = int(item["level"])
        if level >= 6:
            counts["6"] += 1
        elif level == 5:
            counts["5"] += 1
        elif level == 4:
            counts["4"] += 1
        else:
            counts["3"] += 1
    context = {
        "pool_name": pool_name,
        "total": len(items),
        "counts": counts,
        "high": [item for item in items if int(item["level"]) >= 5],
        "signature_code": signature_code,
        "daily_count": daily_count,
        "up_count": up_count,
        "since_up": since_up,
    }
    template = gacha_templates.get_template("bailian_summary.html")
    if GachaSimConfig.get_config("PGRGachaRemoteRenderEnable").data:
        remote = await _render_remote(template.render(**context))
        if remote is not None:
            return remote
    return await _render_html_local(template, context)


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
        up_tag = " [UP]" if result["is_up"] and result["level"] >= 6 else ""
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
