"""战双模拟抽卡 - 数据管理层"""

import time as time_module
import random
from typing import Literal, TypedDict
from pathlib import Path
from datetime import time, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.exc import IntegrityError

from gsuid_core.logger import logger

from .models import (
    PGRGachaPity,
    PGRGachaPool,
    PGRGachaDaily,
    PGRGachaRecord,
    PGRGachaSignature,
)
from .gacha_config import GACHA_SIM_MAIN_PATH
from .gacha_service import PityData, GachaResult, empty_pity

_BEIJING = ZoneInfo("Asia/Shanghai")

# 头像下载失败的内存负缓存（user_id -> 失败时间戳），避免每次抽卡都重试远程
_AVATAR_FAIL_CACHE: dict[str, float] = {}
_AVATAR_FAIL_TTL = 300.0  # 5 分钟内不重试
_AVATAR_FAIL_CACHE_MAX = 2048


def _purge_avatar_fail_cache(now: float) -> None:
    """过期项优先清；仍超额时按时间戳丢掉最旧的一半，防止长期运行无限增长"""
    expired = [key for key, ts in _AVATAR_FAIL_CACHE.items() if now - ts >= _AVATAR_FAIL_TTL]
    for key in expired:
        _AVATAR_FAIL_CACHE.pop(key, None)
    total = len(_AVATAR_FAIL_CACHE)
    if total <= _AVATAR_FAIL_CACHE_MAX:
        return
    # 丢到上限的一半，避免下次插入又立刻触发一次全量排序
    for key, _ in sorted(_AVATAR_FAIL_CACHE.items(), key=lambda item: item[1])[: total // 2]:
        _AVATAR_FAIL_CACHE.pop(key, None)


class RankEntry(TypedDict):
    user_id: str
    draws: int
    ups: int


class DataManager:
    """数据管理器 - 封装数据库操作"""

    @staticmethod
    async def get_all_pity(user_id: str, bot_id: str) -> list[PGRGachaPity]:
        return await PGRGachaPity.get_all_pity(user_id, bot_id)

    @staticmethod
    async def reset_pity(user_id: str, bot_id: str, pool_type: str | None = None) -> int:
        return await PGRGachaPity.reset_pity(user_id, bot_id, pool_type)

    @staticmethod
    async def get_pity_data(user_id: str, bot_id: str, pool_type: str) -> PityData:
        pity = await PGRGachaPity.get_pity(user_id, bot_id, pool_type)
        if pity is None:
            return empty_pity()
        return PityData(
            current_count=pity.current_count,
            sub_count=pity.sub_count,
            guaranteed_up=pity.guaranteed_up,
            total_count=pity.total_count,
            up_count=pity.up_count,
            since_up=pity.since_up,
        )

    @staticmethod
    async def save_pity_data(user_id: str, bot_id: str, pool_type: str, pity: PityData) -> None:
        await PGRGachaPity.save_pity(
            user_id,
            bot_id,
            pool_type,
            pity["current_count"],
            pity["sub_count"],
            pity["guaranteed_up"],
            pity["total_count"],
            pity["up_count"],
            pity["since_up"],
        )

    @staticmethod
    async def get_daily_count(user_id: str, bot_id: str, pool_type: str) -> int:
        return await PGRGachaDaily.get_daily_count(user_id, bot_id, pool_type)

    @staticmethod
    async def add_daily_count(user_id: str, bot_id: str, pool_type: str, count: int) -> None:
        await PGRGachaDaily.add_daily_count(user_id, bot_id, pool_type, count)

    @staticmethod
    async def get_selected_pool(user_id: str, bot_id: str, pool_type: str) -> str | None:
        return await PGRGachaPool.get_selected(user_id, bot_id, pool_type)

    @staticmethod
    async def set_selected_pool(user_id: str, bot_id: str, pool_type: str, pool_id: str) -> None:
        await PGRGachaPool.set_selected(user_id, bot_id, pool_type, pool_id)

    @staticmethod
    async def get_selected_version(user_id: str, bot_id: str) -> str:
        value = await PGRGachaPool.get_selected(user_id, bot_id, "version")
        return value or ""

    @staticmethod
    async def set_selected_version(user_id: str, bot_id: str, version_name: str) -> None:
        await PGRGachaPool.set_selected(user_id, bot_id, "version", version_name)

    @staticmethod
    async def get_owned_names(user_id: str, bot_id: str) -> set[str]:
        """用户已获得过的角色名集合（重复转碎片判定用）"""
        return await PGRGachaRecord.get_owned_names(user_id, bot_id)

    @staticmethod
    async def add_record(user_id: str, bot_id: str, pool_type: str, result: GachaResult) -> None:
        await PGRGachaRecord.add_record(
            user_id,
            bot_id,
            pool_type,
            result["name"],
            str(result["level"]),
            result["category"],
            result["is_up"],
            result["pity_count"],
        )

    @staticmethod
    async def add_records_batch(user_id: str, bot_id: str, pool_type: str, results: list[GachaResult]) -> None:
        """百连场景批量插入，避免逐条 await 带来的 session 开销"""
        if not results:
            return
        await PGRGachaRecord.add_records_batch(
            user_id,
            bot_id,
            pool_type,
            [
                (
                    result["name"],
                    str(result["level"]),
                    result["category"],
                    result["is_up"],
                    result["pity_count"],
                )
                for result in results
            ],
        )

    @staticmethod
    async def get_history(user_id: str, bot_id: str, limit: int = 50) -> list[dict[str, object]]:
        # 稀有度过滤已下推到 SQL，DB 中 4★ 记录只用于重复转碎片判定、不展示
        records = await PGRGachaRecord.get_history(user_id, bot_id, limit)
        return [
            {
                "name": record.name,
                "rarity": record.rarity,
                "is_up": record.is_up,
                "pity_count": record.pity_count,
            }
            for record in records
        ]

    @staticmethod
    async def get_user_stats(user_id: str, bot_id: str) -> dict[str, object]:
        """统计用户的抽卡出货和欧气情况"""
        pities = await PGRGachaPity.get_all_pity(user_id, bot_id)
        stats_rows = await PGRGachaRecord.get_rarity_stats(user_id, bot_id)
        total_pulls = sum(p.total_count for p in pities)
        s_count = 0
        s_pity_sum = 0
        a_count = 0
        for rarity, count, pity_sum in stats_rows:
            if rarity == "6":
                s_count = count
                s_pity_sum = pity_sum
            elif rarity == "5":
                a_count = count
        avg_s_pulls = (s_pity_sum / s_count) if s_count else 0.0
        latest_s = await PGRGachaRecord.get_recent_by_rarity(user_id, bot_id, "6", 5)

        return {
            "total_pulls": total_pulls,
            "s_count": s_count,
            "a_count": a_count,
            "avg_s_pulls": round(avg_s_pulls, 1),
            "s_rate": round(s_count / total_pulls * 100, 2) if total_pulls > 0 else 0.0,
            "latest_s": [{"name": r.name, "pity_count": r.pity_count, "is_up": r.is_up} for r in latest_s],
        }

    @staticmethod
    async def get_signature(user_id: str, bot_id: str) -> str | None:
        return await PGRGachaSignature.get_signature(user_id, bot_id)

    @staticmethod
    async def set_signature(user_id: str, bot_id: str, code: str) -> None:
        await PGRGachaSignature.set_signature(user_id, bot_id, code)

    @staticmethod
    async def check_code_exists(code: str) -> bool:
        return await PGRGachaSignature.check_code_exists(code)

    @staticmethod
    async def generate_signature(user_id: str, bot_id: str) -> str:
        for _ in range(100):
            code = str(random.randint(100000000, 999999999))
            if await PGRGachaSignature.check_code_exists(code):
                continue
            # 每次重试独立事务：撞 unique 时 session 已被标 rollback，必须换一条新的
            try:
                await PGRGachaSignature.set_signature(user_id, bot_id, code)
            except IntegrityError as error:
                # 撞用户维度约束时换码重试没有意义，直接放弃，避免空转 100 次
                if "uq_pgr_sig_bot_user" in str(error.orig):
                    raise ValueError("该用户同时被多个 bot 写入特征码") from error
                continue
            logger.info(f"[战双·模拟抽卡] 用户 {user_id}({bot_id}) 生成特征码: {code}")
            return code
        raise ValueError("无法生成唯一的特征码")

    @staticmethod
    async def get_rank(
        bot_id: str, period: Literal["day", "week", "month"] = "day", limit: int = 10
    ) -> list[RankEntry]:
        today = datetime.now(_BEIJING).date()
        if period == "week":
            start = today - timedelta(days=today.weekday())
        elif period == "month":
            start = today.replace(day=1)
        else:
            start = today
        start_ts = int(datetime.combine(start, time.min, tzinfo=_BEIJING).timestamp())
        end_ts = int(datetime.combine(today, time.max, tzinfo=_BEIJING).timestamp())
        draws = dict(await PGRGachaDaily.sum_draws_by_user(start.strftime("%Y-%m-%d"), bot_id))
        ups = dict(await PGRGachaRecord.count_ups_by_user(start_ts, end_ts, bot_id))
        entries: list[RankEntry] = []
        for user_id in set(draws) | set(ups):
            draw_count = draws[user_id] if user_id in draws else 0
            up_count = ups[user_id] if user_id in ups else 0
            if draw_count <= 0:
                continue
            entries.append({"user_id": user_id, "draws": draw_count, "ups": up_count})
        entries.sort(key=lambda item: (-item["ups"], item["draws"], item["user_id"]))
        return entries[: max(1, min(limit, 50))]

    @staticmethod
    async def get_avatar(user_id: str) -> Path | None:
        if not user_id.isdigit():
            return None
        avatar_dir = GACHA_SIM_MAIN_PATH / "avatars"
        try:
            avatar_dir.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            logger.warning(f"[战双·模拟抽卡] 头像目录创建失败: {error}")
            return None
        dest = avatar_dir / f"{user_id}.jpg"
        # 本地已缓存就直接用，负缓存只拦远程请求，不能连本地文件一起挡掉
        try:
            if dest.exists() and dest.stat().st_size > 0:
                return dest
        except OSError as error:
            logger.warning(f"[战双·模拟抽卡] 头像缓存读取失败: {error}")
            return None
        now = time_module.time()
        fail_ts = _AVATAR_FAIL_CACHE.get(user_id)
        if fail_ts is not None:
            if now - fail_ts < _AVATAR_FAIL_TTL:
                return None
            _AVATAR_FAIL_CACHE.pop(user_id, None)
        if len(_AVATAR_FAIL_CACHE) >= _AVATAR_FAIL_CACHE_MAX:
            _purge_avatar_fail_cache(now)
        url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
        except httpx.HTTPError as error:
            logger.warning(f"[战双·模拟抽卡] 头像下载失败: {error}")
            _AVATAR_FAIL_CACHE[user_id] = time_module.time()
            return None
        content_type = resp.headers.get("content-type", "").lower()
        if resp.status_code != 200 or not resp.content or not content_type.startswith("image/"):
            _AVATAR_FAIL_CACHE[user_id] = time_module.time()
            return None
        try:
            dest.write_bytes(resp.content)
        except OSError as error:
            logger.warning(f"[战双·模拟抽卡] 头像写入失败: {error}")
            return None
        _AVATAR_FAIL_CACHE.pop(user_id, None)
        return dest


data_manager = DataManager()
