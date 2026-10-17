"""战双模拟抽卡 - 数据库模型"""

import time
from typing import Optional
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlmodel import Field, col, select, update
from sqlalchemy import func
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.webconsole.mount_app import PageSchema, GsAdminModel, site
from gsuid_core.utils.database.base_models import BaseIDModel, with_session, with_read_session

_BEIJING = ZoneInfo("Asia/Shanghai")


class PGRGachaPity(BaseIDModel, table=True):
    """保底数据表"""

    __table_args__ = {"extend_existing": True}

    user_id: str = Field(title="用户ID")
    pool_type: str = Field(title="卡池类型")
    current_count: int = Field(default=0, title="最高稀有度计数")
    sub_count: int = Field(default=0, title="次级保底计数")
    guaranteed_up: bool = Field(default=False, title="大保底")
    total_count: int = Field(default=0, title="总抽数")

    @classmethod
    @with_read_session
    async def get_pity(
        cls,
        session: AsyncSession,
        user_id: str,
        pool_type: str,
    ) -> Optional["PGRGachaPity"]:
        stmt = select(cls).where(col(cls.user_id) == user_id, col(cls.pool_type) == pool_type)
        result = await session.execute(stmt)
        return result.scalars().first()

    @classmethod
    @with_session
    async def save_pity(
        cls,
        session: AsyncSession,
        user_id: str,
        pool_type: str,
        current_count: int,
        sub_count: int,
        guaranteed_up: bool,
        total_count: int,
    ) -> None:
        stmt = (
            update(cls)
            .where(col(cls.user_id) == user_id, col(cls.pool_type) == pool_type)
            .values(
                current_count=current_count,
                sub_count=sub_count,
                guaranteed_up=guaranteed_up,
                total_count=total_count,
            )
        )
        result = await session.execute(stmt)
        if (result.rowcount if isinstance(result, CursorResult) else 0) == 0:
            session.add(
                cls(
                    user_id=user_id,
                    pool_type=pool_type,
                    current_count=current_count,
                    sub_count=sub_count,
                    guaranteed_up=guaranteed_up,
                    total_count=total_count,
                )
            )


class PGRGachaRecord(BaseIDModel, table=True):
    """出货记录表"""

    __table_args__ = {"extend_existing": True}

    user_id: str = Field(title="用户ID")
    bot_id: str = Field(default="", title="平台")
    pool_type: str = Field(title="卡池类型")
    name: str = Field(title="名称")
    rarity: str = Field(default="S", title="稀有度")
    item_type: str = Field(default="character", title="类型")
    is_up: bool = Field(default=False, title="是否UP")
    pity_count: int = Field(default=0, title="第几抽")
    created_at: int = Field(default=0, title="时间戳")

    @classmethod
    @with_session
    async def add_record(
        cls,
        session: AsyncSession,
        user_id: str,
        bot_id: str,
        pool_type: str,
        name: str,
        rarity: str,
        item_type: str,
        is_up: bool,
        pity_count: int,
    ) -> None:
        session.add(
            cls(
                user_id=user_id,
                bot_id=bot_id,
                pool_type=pool_type,
                name=name,
                rarity=rarity,
                item_type=item_type,
                is_up=is_up,
                pity_count=pity_count,
                created_at=int(time.time()),
            )
        )

    @classmethod
    @with_read_session
    async def get_history(
        cls,
        session: AsyncSession,
        user_id: str,
        limit: int = 50,
    ) -> list["PGRGachaRecord"]:
        stmt = select(cls).where(col(cls.user_id) == user_id).order_by(col(cls.created_at).desc()).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    @with_read_session
    async def count_ups_by_user(
        cls,
        session: AsyncSession,
        start_ts: int,
        end_ts: int,
    ) -> list[tuple[str, int]]:
        stmt = (
            select(col(cls.user_id), func.count())
            .where(
                col(cls.is_up).is_(True),
                col(cls.item_type) == "character",
                col(cls.rarity) == "6",
                col(cls.created_at) >= start_ts,
                col(cls.created_at) <= end_ts,
            )
            .group_by(col(cls.user_id))
        )
        result = await session.execute(stmt)
        return [(str(row[0]), int(row[1])) for row in result.all()]


class PGRGachaPool(BaseIDModel, table=True):
    """卡池选择表"""

    __table_args__ = {"extend_existing": True}

    user_id: str = Field(title="用户ID")
    pool_type: str = Field(title="卡池类型")
    selected_pool_id: str = Field(default="", title="选中的卡池ID")

    @classmethod
    @with_read_session
    async def get_selected(
        cls,
        session: AsyncSession,
        user_id: str,
        pool_type: str,
    ) -> Optional[str]:
        stmt = select(cls).where(col(cls.user_id) == user_id, col(cls.pool_type) == pool_type)
        result = await session.execute(stmt)
        record = result.scalars().first()
        return record.selected_pool_id if record else None

    @classmethod
    @with_session
    async def set_selected(
        cls,
        session: AsyncSession,
        user_id: str,
        pool_type: str,
        pool_id: str,
    ) -> None:
        stmt = (
            update(cls)
            .where(col(cls.user_id) == user_id, col(cls.pool_type) == pool_type)
            .values(selected_pool_id=pool_id)
        )
        result = await session.execute(stmt)
        if (result.rowcount if isinstance(result, CursorResult) else 0) == 0:
            session.add(
                cls(
                    user_id=user_id,
                    pool_type=pool_type,
                    selected_pool_id=pool_id,
                )
            )


class PGRGachaDaily(BaseIDModel, table=True):
    """每日计数表"""

    __table_args__ = {"extend_existing": True}

    user_id: str = Field(title="用户ID")
    pool_type: str = Field(title="卡池类型")
    date: str = Field(title="日期")
    count: int = Field(default=0, title="抽卡次数")

    @classmethod
    def _today(cls) -> str:
        return datetime.now(_BEIJING).date().strftime("%Y-%m-%d")

    @classmethod
    @with_read_session
    async def get_daily_count(
        cls,
        session: AsyncSession,
        user_id: str,
        pool_type: str,
    ) -> int:
        stmt = select(cls).where(
            col(cls.user_id) == user_id,
            col(cls.pool_type) == pool_type,
            col(cls.date) == cls._today(),
        )
        result = await session.execute(stmt)
        record = result.scalars().first()
        return record.count if record else 0

    @classmethod
    @with_session
    async def add_daily_count(
        cls,
        session: AsyncSession,
        user_id: str,
        pool_type: str,
        count: int,
    ) -> None:
        today = cls._today()
        stmt = (
            update(cls)
            .where(
                col(cls.user_id) == user_id,
                col(cls.pool_type) == pool_type,
                col(cls.date) == today,
            )
            .values(count=cls.count + count)
        )
        result = await session.execute(stmt)
        if (result.rowcount if isinstance(result, CursorResult) else 0) == 0:
            session.add(cls(user_id=user_id, pool_type=pool_type, date=today, count=count))

    @classmethod
    @with_read_session
    async def sum_draws_by_user(
        cls,
        session: AsyncSession,
        since: str,
    ) -> list[tuple[str, int]]:
        stmt = (
            select(col(cls.user_id), func.sum(col(cls.count)))
            .where(col(cls.date) >= since, col(cls.date) <= cls._today())
            .group_by(col(cls.user_id))
        )
        result = await session.execute(stmt)
        return [(str(row[0]), int(row[1] or 0)) for row in result.all()]


class PGRGachaSignature(BaseIDModel, table=True):
    """用户特征码表"""

    __table_args__ = {"extend_existing": True}

    user_id: str = Field(title="用户ID")
    signature_code: str = Field(title="9位数字特征码")

    @classmethod
    @with_read_session
    async def get_signature(
        cls,
        session: AsyncSession,
        user_id: str,
    ) -> Optional[str]:
        stmt = select(cls).where(col(cls.user_id) == user_id)
        result = await session.execute(stmt)
        record = result.scalars().first()
        return record.signature_code if record else None

    @classmethod
    @with_session
    async def set_signature(
        cls,
        session: AsyncSession,
        user_id: str,
        code: str,
    ) -> None:
        stmt = update(cls).where(col(cls.user_id) == user_id).values(signature_code=code)
        result = await session.execute(stmt)
        if (result.rowcount if isinstance(result, CursorResult) else 0) == 0:
            session.add(cls(user_id=user_id, signature_code=code))

    @classmethod
    @with_read_session
    async def check_code_exists(
        cls,
        session: AsyncSession,
        code: str,
    ) -> bool:
        stmt = select(cls).where(col(cls.signature_code) == code)
        result = await session.execute(stmt)
        return result.scalars().first() is not None


@site.register_admin
class PGRGachaPityAdmin(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(label="战双模拟抽卡-保底数据", icon="fa fa-diamond")
    model = PGRGachaPity


@site.register_admin
class PGRGachaRecordAdmin(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(label="战双模拟抽卡-出货记录", icon="fa fa-history")
    model = PGRGachaRecord


@site.register_admin
class PGRGachaPoolAdmin(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(label="战双模拟抽卡-卡池选择", icon="fa fa-exchange")
    model = PGRGachaPool


@site.register_admin
class PGRGachaDailyAdmin(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(label="战双模拟抽卡-每日统计", icon="fa fa-calendar")
    model = PGRGachaDaily


@site.register_admin
class PGRGachaSignatureAdmin(GsAdminModel):
    pk_name = "id"
    page_schema = PageSchema(label="战双模拟抽卡-特征码", icon="fa fa-qrcode")
    model = PGRGachaSignature
