"""战双模拟抽卡 - 命令注册"""

import types
import asyncio
from typing import Literal

from httpx import HTTPError

from gsuid_core.sv import SV, Plugins, get_plugin_available_prefix
from gsuid_core.aps import scheduler
from gsuid_core.bot import Bot
from gsuid_core.logger import logger
from gsuid_core.models import Event
from gsuid_core.segment import MessageSegment

from .data_manager import data_manager
from .gacha_config import GachaSimConfig
from .gacha_update import update_current_pools
from .pool_manager import pool_manager
from .gacha_service import GachaResult, gacha_service
from .draw_gacha_result import (
    _build_items,
    _get_bg_base64,
    _image_to_base64,
    format_text_result,
    render_pool_select,
    render_gacha_result,
    render_gacha_results_batch,
)
from .gacha_help.get_help import get_help

Plugins(name="PGRGachaSim", force_prefix=["pgr", "zs"], allow_empty_prefix=False)

sv_gacha = SV("战双模拟抽卡", priority=1)

_USER_LOCKS: dict[str, asyncio.BoundedSemaphore] = {}
_MAX_CACHED_LOCKS = 1024


def _get_user_lock(uid: str) -> asyncio.BoundedSemaphore:
    if uid in _USER_LOCKS:
        return _USER_LOCKS[uid]
    if len(_USER_LOCKS) >= _MAX_CACHED_LOCKS:
        # 全量清理闲置锁，防止长期运行内存泄漏
        for key in [k for k, v in _USER_LOCKS.items() if not v.locked()]:
            _USER_LOCKS.pop(key)
    if len(_USER_LOCKS) >= _MAX_CACHED_LOCKS:
        # 1024 锁全部在用的极端情况下用临时锁兜底，保证字典不突破上限
        return asyncio.BoundedSemaphore(1)
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


def _format_rarity(rarity: str) -> str:
    mapping = {"6": "6★ [S级]", "5": "5★ [A级]", "4": "4★ [B级]", "3": "3★"}
    return mapping[rarity] if rarity in mapping else rarity


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


def _apply_version_up(pool: dict, pool_type: str, version_data: dict) -> dict:
    """把版本数据里的联动 UP 覆盖到 pool 上，返回新 dict。
    同一大版本（如 V4.8）可能有多个角色，它们共享同一批武器/辅助机 UP，
    所以先从匹配到的 version 提取 patch 前缀，再合并同 patch 下所有 weapon/support。
    """
    updated = pool.copy()
    all_data = pool_manager.get_data()
    versions = all_data["versions"] if "versions" in all_data else []

    # 取 patch 前缀（版本 name 空格前的部分），匹配同 patch 下所有条目
    name = version_data.get("name", "") if isinstance(version_data, dict) else ""
    patch = name.split(" ", 1)[0] if name else ""

    weapon_names: list[str] = []
    support_names: list[str] = []
    for entry in versions:
        if patch and not entry.get("name", "").startswith(patch):
            continue
        w = entry.get("weapon")
        if isinstance(w, str) and w and w not in weapon_names:
            weapon_names.append(w)
        s = entry.get("support")
        if isinstance(s, str) and s and s not in support_names:
            support_names.append(s)

    if pool_type == "weapon" and weapon_names:
        updated["up_w6"] = weapon_names
    elif pool_type == "support_unit" and support_names:
        updated["up_s"] = support_names
    elif pool_type in ("char_theme", "char_fate"):
        # 角色池：版本数据里有 up_a 字段就覆盖 A 级 UP
        up_a = version_data.get("up_a") if isinstance(version_data, dict) else None
        if up_a:
            updated["up_a"] = list(up_a)
    return updated


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

    # 联动当前版本的武器/辅助机/UP A 级：version_name 为空时回退最新版本
    pool_type = pool["type"] if "type" in pool else ""
    all_data = pool_manager.get_data()
    versions = all_data["versions"] if "versions" in all_data else []
    effective_name = version_name
    if not effective_name and versions:
        effective_name = versions[0]["name"] if "name" in versions[0] else ""
    if effective_name and versions:
        version_data = next(
            (entry for entry in versions if "name" in entry and entry["name"] == effective_name),
            None,
        )
        if version_data is not None:
            pool = _apply_version_up(pool, pool_type, version_data)

    uid = ev.user_id
    avatar = await data_manager.get_avatar(uid)
    async with _UserLock(uid):
        if not await _check_daily_limit(bot, ev, pool_type, count):
            return
        pity = await data_manager.get_pity_data(uid, pool_type)
        results = gacha_service.perform_draw(pool, pool_manager.get_data(), pity, count)
        await data_manager.save_pity_data(uid, pool_type, pity)
        await data_manager.add_daily_count(uid, pool_type, count)

        # 在写入本次记录前查询已拥有角色（用于重复转碎片判定）
        owned_names = await data_manager.get_owned_names(uid)
        for result in results:
            if result["level"] >= 5:
                await data_manager.add_record(uid, ev.bot_id, pool_type, result)

        signature_code = await data_manager.get_signature(uid)
        if not signature_code:
            signature_code = await data_manager.generate_signature(uid)
        daily_count = await data_manager.get_daily_count(uid, pool_type)

        pool_name = pool["name"] if "name" in pool else "活动研发"
        image = await render_gacha_result(
            results,
            pool_name,
            signature_code,
            count,
            avatar_path=avatar,
            daily_count=daily_count,
            owned_names=owned_names,
        )
        if image is not None:
            await bot.send(MessageSegment.image(image))
        elif GachaSimConfig.get_config("PGRGachaTextFallback").data:
            await bot.send(format_text_result(results, pool_name, signature_code), bool(ev.group_id))
        else:
            await bot.send("图片渲染失败", bool(ev.group_id))


@sv_gacha.on_fullmatch(("抽卡", "战双抽卡"), block=True)
async def draw_selected(bot: Bot, ev: Event) -> None:
    selected = await data_manager.get_selected_pool(ev.user_id, "gacha") or "char_theme"
    version = await data_manager.get_selected_version(ev.user_id)
    await _do_draw(bot, ev, selected, 10, version)


@sv_gacha.on_fullmatch(("单抽", "战双单抽"), block=True)
async def draw_selected_single(bot: Bot, ev: Event) -> None:
    selected = await data_manager.get_selected_pool(ev.user_id, "gacha") or "char_theme"
    version = await data_manager.get_selected_version(ev.user_id)
    await _do_draw(bot, ev, selected, 1, version)


@sv_gacha.on_fullmatch(("抽卡基准", "战双抽卡基准"), block=True)
async def draw_standard(bot: Bot, ev: Event) -> None:
    await _do_draw(bot, ev, "char_standard", 10)


@sv_gacha.on_fullmatch(("单抽基准", "战双单抽基准"), block=True)
async def draw_standard_single(bot: Bot, ev: Event) -> None:
    await _do_draw(bot, ev, "char_standard", 1)


@sv_gacha.on_fullmatch(("抽卡武器", "战双抽卡武器"), block=True)
async def draw_weapon(bot: Bot, ev: Event) -> None:
    version = await data_manager.get_selected_version(ev.user_id)
    await _do_draw(bot, ev, "weapon", 10, version)


@sv_gacha.on_fullmatch(("单抽武器", "战双单抽武器"), block=True)
async def draw_weapon_single(bot: Bot, ev: Event) -> None:
    version = await data_manager.get_selected_version(ev.user_id)
    await _do_draw(bot, ev, "weapon", 1, version)


@sv_gacha.on_fullmatch(("抽卡辅助机", "战双抽卡辅助机"), block=True)
async def draw_support(bot: Bot, ev: Event) -> None:
    version = await data_manager.get_selected_version(ev.user_id)
    await _do_draw(bot, ev, "support_unit", 10, version)


@sv_gacha.on_fullmatch(("单抽辅助机", "战双单抽辅助机"), block=True)
async def draw_support_single(bot: Bot, ev: Event) -> None:
    version = await data_manager.get_selected_version(ev.user_id)
    await _do_draw(bot, ev, "support_unit", 1, version)


@sv_gacha.on_fullmatch(("抽卡百连辅助机", "战双抽卡百连辅助机"), block=True)
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

    pool_type = pool["type"] if "type" in pool else ""
    all_data = pool_manager.get_data()
    versions = all_data["versions"] if "versions" in all_data else []
    effective_name = version_name
    if not effective_name and versions:
        effective_name = versions[0]["name"] if "name" in versions[0] else ""
    if effective_name and versions:
        version_data = next(
            (entry for entry in versions if "name" in entry and entry["name"] == effective_name),
            None,
        )
        if version_data is not None:
            pool = _apply_version_up(pool, pool_type, version_data)
    uid = ev.user_id
    at_sender = bool(ev.group_id)
    pool_name = pool["name"] if "name" in pool else "活动研发"
    avatar = await data_manager.get_avatar(uid)

    async with _UserLock(uid):
        if not await _check_daily_limit(bot, ev, pool_type, 100):
            return
        # 次数校验通过后再发提示，避免超限时先收到「开始」再收到拒绝
        await bot.send(f"🎰 开始 {pool_name} 百连（10 次十连，共 100 抽），请稍候...", at_sender)
        pity = await data_manager.get_pity_data(uid, pool_type)
        all_results: list[list[GachaResult]] = []
        for _ in range(10):
            all_results.append(gacha_service.perform_draw(pool, pool_manager.get_data(), pity, 10))
        await data_manager.save_pity_data(uid, pool_type, pity)
        await data_manager.add_daily_count(uid, pool_type, 100)
        # 在写入本次记录前查询已拥有角色（用于重复转碎片判定）
        owned_names = await data_manager.get_owned_names(uid)
        for results in all_results:
            for result in results:
                if result["level"] >= 5:
                    await data_manager.add_record(uid, ev.bot_id, pool_type, result)

        signature_code = await data_manager.get_signature(uid)
        if not signature_code:
            signature_code = await data_manager.generate_signature(uid)
        daily_count = await data_manager.get_daily_count(uid, pool_type)

        bg_base64 = _get_bg_base64()
        avatar_b64 = _image_to_base64(avatar) if avatar is not None else ""
        batches = [
            {
                "pool_name": pool_name,
                "draw_type": 10,
                "items": _build_items(res, owned_names),
                "signature_code": signature_code,
                "avatar": avatar_b64,
                "daily_count": daily_count,
                "background": bg_base64,
            }
            for res in all_results
        ]
        images = await render_gacha_results_batch(batches)

        if not images:
            high = [result for results in all_results for result in results if result["level"] >= 5]
            if high:
                lines = [f"【{pool_name} 百连】高稀有度："]
                for result in high[:30]:
                    up_tag = " [UP]" if result["is_up"] else ""
                    rarity_text = _format_rarity(str(result["level"]))
                    lines.append(f"{rarity_text} {result['name']}{up_tag}")
                await bot.send("\n".join(lines), at_sender)
            else:
                await bot.send("百连渲染失败，请稍后重试", at_sender)
            return

        if len(images) == 1:
            await bot.send(MessageSegment.image(images[0]))
        elif GachaSimConfig.get_config("PGRGachaBailianMerge").data and not _is_official_bot(ev):
            await bot.send(MessageSegment.node([MessageSegment.image(image) for image in images]))
        else:
            # 官机不支持合并转发且单条限4图，分批按间隔发送
            batch_size = 4 if _is_official_bot(ev) else 10
            for start in range(0, len(images), batch_size):
                batch = [MessageSegment.image(image) for image in images[start : start + batch_size]]
                await bot.send(batch)
                if _is_official_bot(ev):
                    await asyncio.sleep(0.8)


@sv_gacha.on_fullmatch(("抽卡百连", "战双抽卡百连"), block=True)
async def draw_bailian(bot: Bot, ev: Event) -> None:
    selected = await data_manager.get_selected_pool(ev.user_id, "gacha") or "char_theme"
    version = await data_manager.get_selected_version(ev.user_id)
    await _do_bailian(bot, ev, selected, version)


@sv_gacha.on_fullmatch(("抽卡百连武器", "战双抽卡百连武器"), block=True)
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
    versions = data["versions"] if "versions" in data else []
    for version in versions:
        role = version["role"] if "role" in version else ""
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
        ("char_collab", "联动成员狙击"),
    ):
        options.append((option_id, label))

    version_cards: list[dict[str, object]] = []
    for version in versions:
        role = version["role"] if "role" in version else ""
        if not role:
            continue
        theme_no = 0
        fate_no = 0
        for index, (option_id, _) in enumerate(options, 1):
            if option_id == f"char_theme:{role}":
                theme_no = index
            elif option_id == f"char_fate:{role}":
                fate_no = index
        v_name = version["name"] if "name" in version else ""
        label = f"{v_name} · {role}"
        icon_url = version["icon"] if "icon" in version else ""
        version_cards.append(
            {
                "name": label,
                "figure": icon_url,
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

    old_version = await data_manager.get_selected_version(ev.user_id)
    version_name = ""
    if selected_id.startswith(("char_theme:", "char_fate:")):
        role = selected_id.split(":", 1)[1]
        for version in versions:
            if "role" in version and version["role"] == role:
                version_name = version["name"] if "name" in version else ""
                break
    else:
        # 选非角色池时不清空已有版本，武器/辅助机需要它联动 UP
        version_name = old_version

    await data_manager.set_selected_pool(ev.user_id, "gacha", selected_id)
    await data_manager.set_selected_version(ev.user_id, version_name or old_version)
    await bot.send(
        f"已切换到 {label}，之后 {prefix}抽卡 / {prefix}抽卡武器 / {prefix}抽卡辅助机 使用该版本 UP~",
        at_sender,
    )


@sv_gacha.on_command(("切换卡池", "战双切换卡池"), block=True)
async def switch_pool(bot: Bot, ev: Event) -> None:
    await _switch_pool(bot, ev)


@sv_gacha.on_command(("抽卡排名", "战双抽卡排名"), block=True)
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


@sv_gacha.on_fullmatch(("更新卡池", "战双更新卡池"), block=True)
async def update_pools(bot: Bot, ev: Event) -> None:
    if ev.user_pm is None or ev.user_pm > 0:
        await bot.send("更新卡池仅限主人使用", bool(ev.group_id))
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


@sv_gacha.on_fullmatch(("抽卡记录", "战双抽卡记录"), block=True)
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
        rarity_str = str(record["rarity"])
        rarity_text = _format_rarity(rarity_str)
        pity_num = record["pity_count"]
        lines.append(f"{rarity_text} {record['name']}{up_tag} - 第{pity_num}抽")
    await bot.send("\n".join(lines), at_sender)


@sv_gacha.on_command(("模拟绑定", "战双模拟绑定"), block=True)
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


@sv_gacha.on_fullmatch(("保底", "战双保底", "垫抽", "战双垫抽"), block=True)
async def query_pity(bot: Bot, ev: Event) -> None:
    if not GachaSimConfig.get_config("PGRGachaEnabled").data:
        await bot.send("模拟抽卡功能已关闭", bool(ev.group_id))
        return

    uid = ev.user_id
    at_sender = bool(ev.group_id)
    pities = await data_manager.get_all_pity(uid)
    if not pities:
        await bot.send("你还没有任何抽卡保底记录，快去抽卡试试吧！", at_sender)
        return

    pool_name_map = {
        "char_theme": "主题活动研发",
        "char_fate": "命运主题研发",
        "char_standard": "基准角色研发",
        "weapon": "狙击武器研发",
        "support_unit": "辅助机研发",
    }
    lines = ["【指挥官·研发保底进度】"]
    for p in pities:
        name = pool_name_map.get(p.pool_type, p.pool_type)
        status_line = f"• {name}：当前已垫 {p.current_count} 抽（总抽数 {p.total_count}）"
        if p.guaranteed_up:
            status_line += " [大保底已就绪]"
        lines.append(status_line)

    await bot.send("\n".join(lines), at_sender)


@sv_gacha.on_fullmatch(("重置保底", "战双重置保底"), block=True)
async def reset_user_pity(bot: Bot, ev: Event) -> None:
    if not GachaSimConfig.get_config("PGRGachaEnabled").data:
        await bot.send("模拟抽卡功能已关闭", bool(ev.group_id))
        return

    uid = ev.user_id
    at_sender = bool(ev.group_id)
    count = await data_manager.reset_pity(uid)
    await bot.send(f"已成功为你重置了 {count} 个卡池的垫抽与保底计数！", at_sender)


@sv_gacha.on_fullmatch(("抽卡统计", "战双抽卡统计", "欧气分析", "战双欧气分析"), block=True)
async def query_stats(bot: Bot, ev: Event) -> None:
    if not GachaSimConfig.get_config("PGRGachaEnabled").data:
        await bot.send("模拟抽卡功能已关闭", bool(ev.group_id))
        return

    uid = ev.user_id
    at_sender = bool(ev.group_id)
    stats = await data_manager.get_user_stats(uid)
    total_pulls = stats["total_pulls"] if "total_pulls" in stats else 0
    if not total_pulls:
        await bot.send("暂无抽卡统计数据，先去抽几发吧！", at_sender)
        return

    s_count = stats["s_count"] if "s_count" in stats else 0
    a_count = stats["a_count"] if "a_count" in stats else 0
    avg_s = stats["avg_s_pulls"] if "avg_s_pulls" in stats else 0
    s_rate = stats["s_rate"] if "s_rate" in stats else 0.0

    lines = [
        "【指挥官·模拟抽卡欧气报告】",
        f"• 累计研发：{total_pulls} 抽",
        f"• 获得 S/6★：{s_count} 个（综合出率 {s_rate}%）",
        f"• 获得 A/5★：{a_count} 个",
        f"• 平均 S 抽数：{avg_s} 抽",
    ]
    latest_s = stats["latest_s"] if "latest_s" in stats and isinstance(stats["latest_s"], list) else []
    if latest_s:
        lines.append("\n最近出货记录：")
        for item in latest_s:
            if isinstance(item, dict):
                up_tag = " [UP]" if ("is_up" in item and item["is_up"]) else ""
                name_str = item["name"] if "name" in item else "未知"
                pity_str = item["pity_count"] if "pity_count" in item else "?"
                lines.append(f"  - {name_str}{up_tag} (第 {pity_str} 抽)")

    await bot.send("\n".join(lines), at_sender)


@scheduler.scheduled_job("cron", hour=4, minute=0)
async def _scheduled_sync_pools() -> None:
    """每日凌晨 4 点定时同步官方卡池公告"""
    logger.info("[战双·模拟抽卡] 正在执行每日卡池自动同步...")
    try:
        updated = await update_current_pools()
        if updated:
            pool_manager.reload()
            logger.info("[战双·模拟抽卡] 每日卡池自动同步成功")
        else:
            logger.info("[战双·模拟抽卡] 每日卡池检查完成，暂无更新")
    except (HTTPError, OSError, ValueError) as e:
        logger.error(f"[战双·模拟抽卡] 每日卡池自动同步异常: {e}")


@sv_gacha.on_fullmatch(("模拟抽卡帮助", "战双模拟抽卡帮助", "战双抽卡帮助"), block=True)
async def send_gacha_help(bot: Bot, ev: Event) -> None:
    pm_val = ev.user_pm if ev.user_pm is not None else 6
    await bot.send_option(await get_help(pm_val), [])
