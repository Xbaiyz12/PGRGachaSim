"""战双模拟抽卡 - 命令注册"""

import types
import asyncio
from typing import Literal

from httpx import HTTPError

from gsuid_core.sv import SV, Plugins, get_plugin_available_prefix
from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.segment import MessageSegment

from .data_manager import data_manager
from .gacha_config import GachaSimConfig
from .gacha_update import update_current_pools
from .pool_manager import pool_manager
from .gacha_service import GachaResult, gacha_service
from .draw_gacha_result import format_text_result, render_pool_select, render_gacha_result
from .gacha_help.get_help import get_help

Plugins(name="PGRGachaSim", force_prefix=["pgr", "zs"], allow_empty_prefix=False)

sv_gacha = SV("战双模拟抽卡", priority=1)

_USER_LOCKS: dict[str, asyncio.BoundedSemaphore] = {}


def _get_user_lock(uid: str) -> asyncio.BoundedSemaphore:
    lock = _USER_LOCKS.get(uid)
    if lock is None:
        lock = asyncio.BoundedSemaphore(1)
        _USER_LOCKS[uid] = lock
    return lock


class _UserLock:
    """同一用户同一时间只允许一个抽卡协程"""

    def __init__(self, uid: str) -> None:
        self._lock = _get_user_lock(uid)

    async def __aenter__(self) -> asyncio.BoundedSemaphore:
        await self._lock.acquire()
        return self._lock

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        self._lock.release()


async def _check_daily_limit(bot: Bot, ev: Event, pool_type: str, count: int) -> bool:
    if not GachaSimConfig.get_config("PGRGachaEnabled").data:
        await bot.send("模拟抽卡功能已关闭", bool(ev.group_id))
        return False

    daily_limit = int(GachaSimConfig.get_config("PGRGachaDailyLimit").data)
    if daily_limit <= 0:
        return True

    master_unlimited = GachaSimConfig.get_config("PGRGachaMasterUnlimited").data
    if master_unlimited and ev.user_pm == 0:
        return True

    daily_count = await data_manager.get_daily_count(ev.user_id, pool_type)
    if daily_count + count <= daily_limit:
        return True

    remaining = max(0, daily_limit - daily_count)
    await bot.send(
        f"今日该池已抽 {daily_count} 次，剩余 {remaining} 次，本次需要 {count} 次",
        bool(ev.group_id),
    )
    return False


async def _do_draw(bot: Bot, ev: Event, pool_id: str, count: int, version_name: str = "") -> None:
    if not GachaSimConfig.get_config("PGRGachaEnabled").data:
        await bot.send("模拟抽卡功能已关闭", bool(ev.group_id))
        return

    base_id, _, up_name = pool_id.partition(":")
    pool = pool_manager.get_pool(base_id)
    if pool is None:
        await bot.send("当前没有可用的卡池，请检查 gacha_data.json", bool(ev.group_id))
        return
    if up_name:
        pool = pool.copy()
        pool["up_s"] = [up_name]

    pool_type = pool["type"]
    if version_name:
        version_data = next(
            (entry for entry in pool_manager.get_data()["versions"] if entry.get("name") == version_name),
            None,
        )
        if version_data is not None:
            if pool_type == "weapon" and version_data.get("weapon"):
                pool = pool.copy()
                pool["up_w6"] = [version_data["weapon"]]
            elif pool_type == "support_unit" and version_data.get("support"):
                pool = pool.copy()
                pool["up_s"] = [version_data["support"]]
    uid = ev.user_id
    avatar = await data_manager.get_avatar(uid)
    async with _UserLock(uid):
        if not await _check_daily_limit(bot, ev, pool_type, count):
            return
        pity = await data_manager.get_pity_data(uid, pool_type)
        results = gacha_service.perform_draw(pool, pool_manager.get_data(), pity, count)
        await data_manager.save_pity_data(uid, pool_type, pity)
        await data_manager.add_daily_count(uid, pool_type, count)

        for result in results:
            if result["level"] >= 5:
                await data_manager.add_record(uid, ev.bot_id, pool_type, result)

        signature_code = await data_manager.get_signature(uid)
        if not signature_code:
            signature_code = await data_manager.generate_signature(uid)
        daily_count = await data_manager.get_daily_count(uid, pool_type)

        image = await render_gacha_result(
            results, pool["name"], signature_code, count, avatar_path=avatar, daily_count=daily_count
        )
        if image is not None:
            await bot.send(MessageSegment.image(image))
        elif GachaSimConfig.get_config("PGRGachaTextFallback").data:
            await bot.send(format_text_result(results, pool["name"], signature_code), bool(ev.group_id))
        else:
            await bot.send("图片渲染失败", bool(ev.group_id))


@sv_gacha.on_fullmatch(("抽卡",), block=True)
async def draw_selected(bot: Bot, ev: Event) -> None:
    selected = await data_manager.get_selected_pool(ev.user_id, "gacha") or "char_theme"
    await _do_draw(bot, ev, selected, 10)


@sv_gacha.on_fullmatch(("抽卡基准",), block=True)
async def draw_standard(bot: Bot, ev: Event) -> None:
    await _do_draw(bot, ev, "char_standard", 10)


@sv_gacha.on_fullmatch(("抽卡武器",), block=True)
async def draw_weapon(bot: Bot, ev: Event) -> None:
    version = await data_manager.get_selected_version(ev.user_id)
    await _do_draw(bot, ev, "weapon", 10, version)


@sv_gacha.on_fullmatch(("抽卡辅助机",), block=True)
async def draw_support(bot: Bot, ev: Event) -> None:
    version = await data_manager.get_selected_version(ev.user_id)
    await _do_draw(bot, ev, "support_unit", 10, version)


@sv_gacha.on_fullmatch(("抽卡百连辅助机",), block=True)
async def draw_bailian_support(bot: Bot, ev: Event) -> None:
    version = await data_manager.get_selected_version(ev.user_id)
    await _do_bailian(bot, ev, "support_unit", version)


def _is_official_bot(ev: Event) -> bool:
    return "official" in ev.bot_id.lower()


async def _do_bailian(bot: Bot, ev: Event, pool_id: str, version_name: str = "") -> None:
    if not GachaSimConfig.get_config("PGRGachaEnabled").data:
        await bot.send("模拟抽卡功能已关闭", bool(ev.group_id))
        return
    if not GachaSimConfig.get_config("PGRGachaEnableBailian").data:
        await bot.send("百连功能未开启，请联系管理员在 WebConsole 开启", bool(ev.group_id))
        return

    base_id, _, up_name = pool_id.partition(":")
    pool = pool_manager.get_pool(base_id)
    if pool is None:
        await bot.send("当前没有可用的卡池，请检查 gacha_data.json", bool(ev.group_id))
        return
    if up_name:
        pool = pool.copy()
        pool["up_s"] = [up_name]

    pool_type = pool["type"]
    if version_name:
        version_data = next(
            (entry for entry in pool_manager.get_data()["versions"] if entry.get("name") == version_name),
            None,
        )
        if version_data is not None:
            if pool_type == "weapon" and version_data.get("weapon"):
                pool = pool.copy()
                pool["up_w6"] = [version_data["weapon"]]
            elif pool_type == "support_unit" and version_data.get("support"):
                pool = pool.copy()
                pool["up_s"] = [version_data["support"]]
    uid = ev.user_id
    at_sender = bool(ev.group_id)
    await bot.send(f"🎰 开始 {pool['name']} 百连（10 次十连，共 100 抽），请稍候...", at_sender)
    avatar = await data_manager.get_avatar(uid)

    async with _UserLock(uid):
        if not await _check_daily_limit(bot, ev, pool_type, 100):
            return
        pity = await data_manager.get_pity_data(uid, pool_type)
        all_results: list[list[GachaResult]] = []
        for _ in range(10):
            all_results.append(gacha_service.perform_draw(pool, pool_manager.get_data(), pity, 10))
        await data_manager.save_pity_data(uid, pool_type, pity)
        await data_manager.add_daily_count(uid, pool_type, 100)
        for results in all_results:
            for result in results:
                if result["level"] >= 5:
                    await data_manager.add_record(uid, ev.bot_id, pool_type, result)

        signature_code = await data_manager.get_signature(uid)
        if not signature_code:
            signature_code = await data_manager.generate_signature(uid)
        daily_count = await data_manager.get_daily_count(uid, pool_type)

        images: list[bytes] = []
        for results in all_results:
            image = await render_gacha_result(
                results, pool["name"], signature_code, 10, avatar_path=avatar, daily_count=daily_count
            )
            if image is not None:
                images.append(image)

        if not images:
            high = [result for results in all_results for result in results if result["level"] >= 5]
            if high:
                lines = [f"【{pool['name']} 百连】高稀有度："]
                for result in high[:30]:
                    up_tag = " [UP]" if result["is_up"] else ""
                    lines.append(f"{result['level']} {result['name']}{up_tag}")
                await bot.send("\n".join(lines), at_sender)
            else:
                await bot.send("百连渲染失败，请稍后重试", at_sender)
            return

        if len(images) == 1:
            await bot.send(MessageSegment.image(images[0]))
        elif GachaSimConfig.get_config("PGRGachaBailianMerge").data and not _is_official_bot(ev):
            await bot.send(MessageSegment.node([MessageSegment.image(image) for image in images]))
        else:
            # QQ 官机不支持合并转发，且单条消息最多 4 张图，分批发送
            batch_size = 4 if _is_official_bot(ev) else 10
            for start in range(0, len(images), batch_size):
                batch = [MessageSegment.image(image) for image in images[start : start + batch_size]]
                await bot.send(batch)
                if _is_official_bot(ev):
                    await asyncio.sleep(0.8)


@sv_gacha.on_fullmatch(("抽卡百连",), block=True)
async def draw_bailian(bot: Bot, ev: Event) -> None:
    selected = await data_manager.get_selected_pool(ev.user_id, "gacha") or "char_theme"
    await _do_bailian(bot, ev, selected)


@sv_gacha.on_fullmatch(("抽卡百连武器",), block=True)
async def draw_bailian_weapon(bot: Bot, ev: Event) -> None:
    version = await data_manager.get_selected_version(ev.user_id)
    await _do_bailian(bot, ev, "weapon", version)


async def _switch_pool(bot: Bot, ev: Event) -> None:
    if not GachaSimConfig.get_config("PGRGachaEnabled").data:
        await bot.send("模拟抽卡功能已关闭", bool(ev.group_id))
        return

    at_sender = bool(ev.group_id)
    prefix = get_plugin_available_prefix("PGRGachaSim")
    data = pool_manager.get_data()
    options: list[tuple[str, str]] = []

    version_roles: list[str] = []
    for version in data.get("versions", []):
        role = version.get("role") or ""
        if role and role not in version_roles:
            version_roles.append(role)
    for name in version_roles:
        options.append((f"char_theme:{name}", f"{name}-主题研发"))
        options.append((f"char_fate:{name}", f"{name}-命运主题研发"))

    for option_id, label in (
        ("char_standard", "基准研发"),
        ("weapon", "武器研发"),
        ("weapon_collab", "联动武器狙击"),
        ("support_unit", "辅助机研发"),
    ):
        options.append((option_id, label))

    version_cards: list[dict[str, object]] = []
    for version in data.get("versions", []):
        role = version.get("role") or ""
        if not role:
            continue
        theme_no = 0
        fate_no = 0
        for index, (option_id, _) in enumerate(options, 1):
            if option_id == f"char_theme:{role}":
                theme_no = index
            elif option_id == f"char_fate:{role}":
                fate_no = index
        label = f"{version.get('name', '')} · {role}"
        version_cards.append(
            {
                "name": label,
                "figure": version.get("icon", ""),
                "theme_no": theme_no,
                "fate_no": fate_no,
            }
        )

    current = await data_manager.get_selected_pool(ev.user_id, "gacha") or "char_theme"
    text = ev.text.strip() if ev.text else ""
    if not text:
        image = await render_pool_select(version_cards, prefix, "版本卡池") if version_cards else None
        if image is not None:
            await bot.send(MessageSegment.image(image))
            specials = [
                (index, label)
                for index, (option_id, label) in enumerate(options, 1)
                if not option_id.startswith(("char_theme:", "char_fate:"))
            ]
            lines = ["其他卡池："]
            for index, label in specials:
                mark = "（当前）" if options[index - 1][0] == current else ""
                lines.append(f"{index}. {label}{mark}")
            lines.append(f"\n回复 {prefix}切换卡池 编号 选择，之后 {prefix}抽卡 使用选中卡池")
            await bot.send("\n".join(lines), at_sender)
            return
        lines = [f"当前可选卡池（共 {len(options)} 个）："]
        for index, (option_id, label) in enumerate(options, 1):
            mark = "（当前）" if option_id == current else ""
            lines.append(f"{index}. {label}{mark}")
        lines.append(f"\n回复 {prefix}切换卡池 编号 选择，之后 {prefix}抽卡 使用选中卡池")
        await bot.send("\n".join(lines), at_sender)
        return

    selected_id = ""
    if text.isdigit():
        index = int(text)
        if 1 <= index <= len(options):
            selected_id = options[index - 1][0]
    else:
        for option_id, label in options:
            if text in label:
                selected_id = option_id
                break

    if not selected_id:
        await bot.send(f"未找到匹配 '{text}' 的卡池，请检查后重试~", at_sender)
        return

    label = next(label for option_id, label in options if option_id == selected_id)
    await data_manager.set_selected_pool(ev.user_id, "gacha", selected_id)
    version_name = ""
    if selected_id.startswith("char_theme:"):
        role = selected_id.split(":", 1)[1]
        for version in pool_manager.get_data()["versions"]:
            if version.get("role") == role:
                version_name = version.get("name", "")
                break
    await data_manager.set_selected_version(ev.user_id, version_name)
    await bot.send(
        f"已切换到 {label}，之后 {prefix}抽卡 / {prefix}抽卡武器 / {prefix}抽卡辅助机 使用该版本 UP~",
        at_sender,
    )


@sv_gacha.on_command(("切换卡池",), block=True)
async def switch_pool(bot: Bot, ev: Event) -> None:
    await _switch_pool(bot, ev)


@sv_gacha.on_command(("抽卡排名",), block=True)
async def draw_rank(bot: Bot, ev: Event) -> None:
    if not GachaSimConfig.get_config("PGRGachaEnabled").data:
        await bot.send("模拟抽卡功能已关闭", bool(ev.group_id))
        return
    text = ev.text.strip() if ev.text else ""
    period: Literal["day", "week", "month"] = "day"
    if "周" in text:
        period = "week"
    elif "月" in text:
        period = "month"
    rows = await data_manager.get_rank(period, 10)
    if not rows:
        await bot.send("暂无排名数据，先抽几发再来吧~", bool(ev.group_id))
        return
    if period == "week":
        label = "周"
    elif period == "month":
        label = "月"
    else:
        label = "天"
    lines = [f"【抽卡排名 · 按{label}】", "排名规则：UP 角色（S 级）越多、抽数越少排名越高"]
    for index, row in enumerate(rows, 1):
        lines.append(f"{index}. {row['user_id']}  抽数 {row['draws']}  UP {row['ups']}")
    await bot.send("\n".join(lines), bool(ev.group_id))


@sv_gacha.on_fullmatch(("更新卡池",), block=True)
async def update_pools(bot: Bot, ev: Event) -> None:
    if ev.user_pm is None or ev.user_pm > 0:
        return
    at_sender = bool(ev.group_id)
    await bot.send("正在从官方公告更新当期卡池...", at_sender)
    try:
        names = await update_current_pools()
    except (HTTPError, ValueError, KeyError) as error:
        logger.error(f"[战双·模拟抽卡] 更新卡池失败: {error}")
        await bot.send(f"更新卡池失败: {error}", at_sender)
        return
    if not names:
        await bot.send("未找到当期研发公告，卡池未变化", at_sender)
        return
    await bot.send(f"卡池已更新，当期 UP: {'、'.join(names)}", at_sender)


@sv_gacha.on_fullmatch(("抽卡记录",), block=True)
async def draw_record(bot: Bot, ev: Event) -> None:
    if not GachaSimConfig.get_config("PGRGachaEnabled").data:
        await bot.send("模拟抽卡功能已关闭", bool(ev.group_id))
        return

    records = await data_manager.get_history(ev.user_id, limit=100)
    at_sender = bool(ev.group_id)
    if not records:
        await bot.send("暂无抽卡记录，快去抽卡吧~\n使用「模拟抽卡帮助」查看命令！", at_sender)
        return

    lines = [f"共 {len(records)} 条高稀有度记录："]
    for record in records[:20]:
        up_tag = " [UP]" if record["is_up"] else ""
        lines.append(f"{record['rarity']} {record['name']}{up_tag} - 第{record['pity_count']}抽")
    await bot.send("\n".join(lines), at_sender)


@sv_gacha.on_command(("模拟绑定",), block=True)
async def bind_signature(bot: Bot, ev: Event) -> None:
    uid = ev.user_id
    user_input = ev.text.strip() if ev.text else ""
    at_sender = bool(ev.group_id)

    if not user_input:
        current = await data_manager.get_signature(uid)
        if current:
            await bot.send(f"你的当前特征码: {current}\n\n如需更换，请回复 模拟绑定123456789", at_sender)
        else:
            await bot.send("你还没有特征码，首次抽卡时会自动生成~\n\n如需手动生成，请回复 模拟绑定123456789", at_sender)
        return

    if not user_input.isdigit() or len(user_input) != 9:
        await bot.send("特征码必须是9位数字！", at_sender)
        return

    if await data_manager.check_code_exists(user_input):
        current = await data_manager.get_signature(uid)
        if current == user_input:
            await bot.send(f"这个特征码已经是你的了，当前特征码: {user_input}", at_sender)
        else:
            await bot.send("该特征码已被其他用户绑定，请更换一个~", at_sender)
        return

    await data_manager.set_signature(uid, user_input)
    await bot.send(f"特征码绑定成功！\n你的特征码: {user_input}", at_sender)


@sv_gacha.on_fullmatch(("模拟抽卡帮助",), block=True)
async def send_gacha_help(bot: Bot, ev: Event) -> None:
    await bot.send_option(await get_help(ev.user_pm), [])
