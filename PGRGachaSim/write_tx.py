"""插件内自带的跨表写事务。

框架的 ``@with_session`` 每条语句各自成事务，而抽卡的「保底 / 日限 / 记录」三段写
必须同进同退，否则会出现垫抽没存、日限扣了记录没写。这里不借用框架的内部机制，
自己开 session、自己提交，三条写方法改为接收外部 session。
"""

import asyncio
import contextvars
from typing import TypeVar, AsyncIterator
from contextlib import asynccontextmanager
from collections.abc import Callable, Awaitable

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from gsuid_core.utils.database.write_gate import sqlite_write_gate
from gsuid_core.utils.database import base_models

GACHA_TX_LABEL = "pgr_gacha_draw"
# 撞上瞬时锁竞争时整段重跑的次数；事务已回滚，重入不会留下半截数据
GACHA_TX_ATTEMPTS = 3

_T = TypeVar("_T")

# 当前事务的会话。只在写方法内部读取，事务结束即失效。
_CURRENT_SESSION: contextvars.ContextVar[AsyncSession | None] = contextvars.ContextVar(
    "pgr_gacha_session",
    default=None,
)


class TransientTransactionError(Exception):
    """事务已回滚且原因是瞬时锁竞争：调用方可以整段重跑。"""


def _is_transient(error: BaseException) -> bool:
    """只有 SQLite 锁竞争 / 瞬时 I/O 才值得重跑。"""
    if not isinstance(error, OperationalError):
        return False
    message = str(error).lower()
    if "unable to open database file" in message:
        return False
    return "locked" in message or "busy" in message or "disk i/o error" in message


def current_session() -> AsyncSession:
    """取当前事务的会话；不在事务里说明调用方写错了，直接报错而不是静默失效。"""
    session = _CURRENT_SESSION.get()
    if session is None:
        raise RuntimeError("gacha_write_transaction 之外不能写保底/日限/记录")
    return session


@asynccontextmanager
async def _write_transaction() -> AsyncIterator[None]:
    # 走模块属性取值：init_database() 会重新绑定 async_maker，不能导入时就快照下来
    async with base_models.async_maker() as session:
        token = _CURRENT_SESSION.set(session)
        try:
            yield
            await session.commit()
        except BaseException as error:
            await session.rollback()
            if _is_transient(error):
                raise TransientTransactionError(str(error)) from error
            raise
        finally:
            _CURRENT_SESSION.reset(token)
            await session.close()


@asynccontextmanager
async def gacha_write_transaction() -> AsyncIterator[None]:
    """占住 SQLite 单写者闸门并开一条会话，供内部多条写语句共用。"""
    if base_models._db_type != "sqlite":
        async with _write_transaction():
            yield
        return
    async with sqlite_write_gate.hold(core=True, label=GACHA_TX_LABEL):
        async with _write_transaction():
            yield


async def run_gacha_transaction(action: Callable[[], Awaitable[_T]]) -> _T:
    """在事务里执行 ``action``，撞瞬时锁竞争就整段重来。"""
    last_error: TransientTransactionError | None = None
    for attempt in range(GACHA_TX_ATTEMPTS):
        try:
            async with gacha_write_transaction():
                return await action()
        except TransientTransactionError as error:
            last_error = error
            if attempt >= GACHA_TX_ATTEMPTS - 1:
                break
            await asyncio.sleep(0.5 * (2**attempt))
    raise TransientTransactionError("写事务重试耗尽") from last_error
