"""战双模拟抽卡 - 数据管理层"""

import random
from typing import Literal, TypedDict
from pathlib import Path
from datetime import time, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from gsuid_core.logger import logger

from .models import PGRGachaPity, PGRGachaPool, PGRGachaDaily, PGRGachaRecord, PGRGachaSignature
from .gacha_config import GACHA_SIM_MAIN_PATH
from .gacha_service import PityData, GachaResult, empty_pity

_BEIJING = ZoneInfo("Asia/Shanghai")


class RankEntry(TypedDict):
    user_id: str
    draws: int
    ups: int


class DataManager:
    """数据管理器 - 封装数据库操作"""

    @staticmethod
    async def get_pity_data(user_id: str, pool_type: str) -> PityData:
        pity = await PGRGachaPity.get_pity(user_id, pool_type)
        if pity is None:
            return empty_pity()
        return PityData(
            current_count=pity.current_count,
            sub_count=pity.sub_count,
            guaranteed_up=pity.guaranteed_up,
            total_count=pity.total_count,
        )

    @staticmethod
    async def save_pity_data(user_id: str, pool_type: str, pity: PityData) -> None:
        await PGRGachaPity.save_pity(
            user_id,
            pool_type,
            pity["current_count"],
            pity["sub_count"],
            pity["guaranteed_up"],
            pity["total_count"],
        )

    @staticmethod
    async def get_daily_count(user_id: str, pool_type: str) -> int:
        return await PGRGachaDaily.get_daily_count(user_id, pool_type)

    @staticmethod
    async def add_daily_count(user_id: str, pool_type: str, count: int) -> None:
        await PGRGachaDaily.add_daily_count(user_id, pool_type, count)

    @staticmethod
    async def get_selected_pool(user_id: str, pool_type: str) -> str | None:
        return await PGRGachaPool.get_selected(user_id, pool_type)

    @staticmethod
    async def set_selected_pool(user_id: str, pool_type: str, pool_id: str) -> None:
        await PGRGachaPool.set_selected(user_id, pool_type, pool_id)

    @staticmethod
    async def get_selected_version(user_id: str) -> str:
        value = await PGRGachaPool.get_selected(user_id, "version")
        return value or ""

    @staticmethod
    async def set_selected_version(user_id: str, version_name: str) -> None:
        await PGRGachaPool.set_selected(user_id, "version", version_name)

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
    async def get_history(user_id: str, limit: int = 50) -> list[dict[str, object]]:
        records = await PGRGachaRecord.get_history(user_id, limit)
        return [
            {
                "name": record.name,
                "rarity": record.rarity,
                "item_type": record.item_type,
                "is_up": record.is_up,
                "pity_count": record.pity_count,
                "pool_type": record.pool_type,
                "created_at": record.created_at,
            }
            for record in records
        ]

    @staticmethod
    async def get_signature(user_id: str) -> str | None:
        return await PGRGachaSignature.get_signature(user_id)

    @staticmethod
    async def set_signature(user_id: str, code: str) -> None:
        await PGRGachaSignature.set_signature(user_id, code)

    @staticmethod
    async def check_code_exists(code: str) -> bool:
        return await PGRGachaSignature.check_code_exists(code)

    @staticmethod
    async def generate_signature(user_id: str) -> str:
        for _ in range(100):
            code = str(random.randint(100000000, 999999999))
            if not await PGRGachaSignature.check_code_exists(code):
                await PGRGachaSignature.set_signature(user_id, code)
                logger.info(f"[战双·模拟抽卡] 用户 {user_id} 生成特征码: {code}")
                return code
        raise ValueError("无法生成唯一的特征码")

    @staticmethod
    async def get_rank(period: Literal["day", "week", "month"] = "day", limit: int = 10) -> list[RankEntry]:
        today = datetime.now(_BEIJING).date()
        if period == "week":
            start = today - timedelta(days=today.weekday())
        elif period == "month":
            start = today.replace(day=1)
        else:
            start = today
        start_ts = int(datetime.combine(start, time.min, tzinfo=_BEIJING).timestamp())
        end_ts = int(datetime.combine(today, time.max, tzinfo=_BEIJING).timestamp())
        draws = dict(await PGRGachaDaily.sum_draws_by_user(start.strftime("%Y-%m-%d")))
        ups = dict(await PGRGachaRecord.count_ups_by_user(start_ts, end_ts))
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
        try:
            if dest.exists() and dest.stat().st_size > 0:
                return dest
        except OSError as error:
            logger.warning(f"[战双·模拟抽卡] 头像缓存读取失败: {error}")
            return None
        url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=100"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
        except httpx.HTTPError as error:
            logger.warning(f"[战双·模拟抽卡] 头像下载失败: {error}")
            return None
        content_type = resp.headers.get("content-type", "").lower()
        if resp.status_code != 200 or not resp.content or not content_type.startswith("image/"):
            return None
        try:
            dest.write_bytes(resp.content)
        except OSError as error:
            logger.warning(f"[战双·模拟抽卡] 头像写入失败: {error}")
            return None
        return dest


data_manager = DataManager()
