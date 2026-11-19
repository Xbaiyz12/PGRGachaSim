"""战双模拟抽卡 - 卡池数据加载（数据来自 BWIKI 新抽卡模拟器）"""

import json
import time
import shutil
import hashlib
from typing import Callable
from pathlib import Path

from gsuid_core.logger import logger
from gsuid_core.data_store import get_res_path

from .gacha_service import PoolData, GachaData, GachaItem, VersionData

PLUGIN_DIR = Path(__file__).parent
CONFIG_DIR = PLUGIN_DIR / "config"
BUILTIN_DATA_FILE = CONFIG_DIR / "gacha_data.json"
ICON_DIR = PLUGIN_DIR / "texture2d"

DATA_DIR = get_res_path() / "PGRGachaSim"
DATA_DIR.mkdir(parents=True, exist_ok=True)
USER_DATA_FILE = DATA_DIR / "gacha_data.json"

_ICON_SUBDIRS = ("角色", "角色碎片", "武器", "辅助机", "材料")
_ICON_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
# 卡池数据加载失败后的重试间隔（秒）
_LOAD_RETRY_S = 30.0

# 渲染层把「换图后要清的 base64 缓存」挂进来，避免本模块反向依赖绘图模块
_icon_reload_hooks: list[Callable[[], None]] = []


def register_icon_reload_hook(hook: Callable[[], None]) -> None:
    """注册 reload 时要一起清掉的缓存清理函数"""
    if hook not in _icon_reload_hooks:
        _icon_reload_hooks.append(hook)


def _parse_items(raw: object) -> list[GachaItem]:
    if not isinstance(raw, list):
        return []
    items: list[GachaItem] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        item = GachaItem(name=name)
        for key in ("mainIcon", "shardsIcon", "figure"):
            value = entry.get(key)
            if not isinstance(value, str) or not value:
                continue
            if key == "mainIcon":
                item["mainIcon"] = value
            elif key == "shardsIcon":
                item["shardsIcon"] = value
            else:
                item["figure"] = value
        items.append(item)
    return items


def _parse_group(raw: object) -> dict[str, list[GachaItem]]:
    result: dict[str, list[GachaItem]] = {}
    if not isinstance(raw, dict):
        return result
    for key, value in raw.items():
        items = _parse_items(value)
        if items:
            result[str(key)] = items
    return result


def _parse_data(raw: object) -> GachaData | None:
    if not isinstance(raw, dict):
        return None
    return GachaData(
        characters=_parse_group(raw.get("characters")),
        weapons=_parse_group(raw.get("weapons")),
        materials=_parse_group(raw.get("materials")),
        support=_parse_group(raw.get("support")),
        versions=_parse_versions(raw.get("versions")),
        standard_s=_parse_str_list(raw.get("standard_s")),
    )


def _parse_str_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, str) and entry]


def _parse_versions(raw: object) -> list[VersionData]:
    result: list[VersionData] = []
    if not isinstance(raw, list):
        return result
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        version = VersionData(name=name)
        icon = entry.get("icon")
        if isinstance(icon, str) and icon:
            version["icon"] = icon
        role = entry.get("role")
        if isinstance(role, str) and role:
            version["role"] = role
        weapon = entry.get("weapon")
        if isinstance(weapon, str) and weapon:
            version["weapon"] = weapon
        support = entry.get("support")
        if isinstance(support, str) and support:
            version["support"] = support
        if entry.get("rerun") is True:
            version["rerun"] = True
        result.append(version)
    return result


def _parse_pool(pool_id: str, raw: object) -> PoolData | None:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    pool_type = raw.get("type")
    if not isinstance(name, str) or not isinstance(pool_type, str):
        return None
    pool = PoolData(id=pool_id, name=name, type=pool_type)
    target = raw.get("target")
    if isinstance(target, str):
        pool["target"] = target
    total = raw.get("total")
    if isinstance(total, int) and not isinstance(total, bool):
        pool["total"] = total
    weights_raw = raw.get("weights")
    if isinstance(weights_raw, dict):
        weights: dict[str, float] = {}
        for key, value in weights_raw.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                weights[str(key)] = float(value)
        if weights:
            pool["weights"] = weights
    for key in ("up_s", "up_w6"):
        names = _parse_str_list(raw.get(key))
        if names:
            if key == "up_s":
                pool["up_s"] = names
            else:
                pool["up_w6"] = names
    for key in ("up_rate_s", "up_rate_w6"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if key == "up_rate_s":
                pool["up_rate_s"] = float(value)
            else:
                pool["up_rate_w6"] = float(value)
    return pool


class PoolManager:
    """卡池管理器"""

    def __init__(self) -> None:
        self._data: GachaData | None = None
        self._pools: dict[str, PoolData] = {}
        self._loaded = False
        # md5(url) -> 本地图标路径，首次查询时建表，避免每次十连都做几十次 exists()
        self._icon_index: dict[str, Path] | None = None
        # 加载失败后的退避：文件长期缺失时不要每次都重新 stat/读盘
        self._next_load_retry = 0.0

    def _load(self) -> None:
        if self._loaded:
            return
        if self._next_load_retry > time.monotonic():
            return
        if not USER_DATA_FILE.exists() and BUILTIN_DATA_FILE.exists():
            shutil.copyfile(BUILTIN_DATA_FILE, USER_DATA_FILE)
            logger.info(f"[战双·模拟抽卡] 已生成卡池数据: {USER_DATA_FILE}")

        path = USER_DATA_FILE if USER_DATA_FILE.exists() else BUILTIN_DATA_FILE
        if not path.exists():
            # 不置 _loaded，退避到期后重试，文件补上即可恢复
            self._next_load_retry = time.monotonic() + _LOAD_RETRY_S
            logger.warning("[战双·模拟抽卡] 未找到 gacha_data.json，卡池为空")
            return

        try:
            with open(path, "r", encoding="utf-8") as file:
                raw = json.load(file)
        # UnicodeDecodeError（编码写坏）不是 OSError，漏了会让每抽都崩
        except (OSError, ValueError) as error:
            self._next_load_retry = time.monotonic() + _LOAD_RETRY_S
            logger.warning(f"[战双·模拟抽卡] 读取卡池数据失败: {error}")
            return

        # 先解析到局部变量，成功后再整体替换，避免 reload 中途抛错留下半截数据
        pools: dict[str, PoolData] = {}
        if isinstance(raw, dict):
            pools_raw = raw.get("pools")
            if isinstance(pools_raw, dict):
                for pool_id, pool_raw in pools_raw.items():
                    pool = _parse_pool(str(pool_id), pool_raw)
                    if pool is not None:
                        pools[str(pool_id)] = pool
        self._data = _parse_data(raw)
        self._pools = pools
        self._loaded = True
        logger.info(f"[战双·模拟抽卡] 卡池数据加载完成: {len(self._pools)} 个卡池")

    def get_data(self) -> GachaData:
        self._load()
        if self._data is None:
            return GachaData(characters={}, weapons={}, materials={}, support={}, versions=[], standard_s=[])
        return self._data

    def get_pool(self, pool_id: str) -> PoolData | None:
        self._load()
        return self._pools.get(pool_id)

    def reload(self) -> None:
        """清空缓存并重新读取数据（供更新卡池调用）"""
        self._data = None
        self._pools = {}
        self._loaded = False
        self._icon_index = None
        # 手动 reload 必须绕过失败退避，否则退避窗口内点什么都没反应
        self._next_load_retry = 0.0
        for hook in _icon_reload_hooks:
            hook()
        self._load()

    def _build_icon_index(self) -> dict[str, Path]:
        index: dict[str, Path] = {}
        if not ICON_DIR.exists():
            return index
        # 子目录优先：先写分类目录，根目录同名文件只在缺失时补位
        for sub in (*_ICON_SUBDIRS, ""):
            folder = ICON_DIR / sub if sub else ICON_DIR
            if not folder.is_dir():
                continue
            try:
                entries = list(folder.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.suffix.lower() not in _ICON_SUFFIXES:
                    continue
                index.setdefault(entry.stem, entry)
        return index

    def find_icon_path(self, url: str) -> Path | None:
        """按图标 URL 查本地缓存文件（文件名 = md5(url) + 扩展名）"""
        if not url.startswith("http"):
            return None
        if self._icon_index is None:
            self._icon_index = self._build_icon_index()
        return self._icon_index.get(hashlib.md5(url.encode()).hexdigest())


pool_manager = PoolManager()
