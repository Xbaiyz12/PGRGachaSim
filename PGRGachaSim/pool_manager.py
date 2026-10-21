"""战双模拟抽卡 - 卡池数据加载（数据来自 BWIKI 新抽卡模拟器）"""

import json
import shutil
import hashlib
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
    for key in ("up_s", "up_a", "up_w6"):
        names = _parse_str_list(raw.get(key))
        if names:
            if key == "up_s":
                pool["up_s"] = names
            elif key == "up_a":
                pool["up_a"] = names
            else:
                pool["up_w6"] = names
    for key in ("up_rate_s", "up_rate_a", "up_rate_w6"):
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if key == "up_rate_s":
                pool["up_rate_s"] = float(value)
            elif key == "up_rate_a":
                pool["up_rate_a"] = float(value)
            else:
                pool["up_rate_w6"] = float(value)
    return pool


class PoolManager:
    """卡池管理器"""

    def __init__(self) -> None:
        self._data: GachaData | None = None
        self._pools: dict[str, PoolData] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        if not USER_DATA_FILE.exists() and BUILTIN_DATA_FILE.exists():
            shutil.copyfile(BUILTIN_DATA_FILE, USER_DATA_FILE)
            logger.info(f"[战双·模拟抽卡] 已生成卡池数据: {USER_DATA_FILE}")

        path = USER_DATA_FILE if USER_DATA_FILE.exists() else BUILTIN_DATA_FILE
        if not path.exists():
            logger.warning("[战双·模拟抽卡] 未找到 gacha_data.json，卡池为空")
            self._loaded = True
            return

        try:
            with open(path, "r", encoding="utf-8") as file:
                raw = json.load(file)
        except (OSError, json.JSONDecodeError) as error:
            logger.warning(f"[战双·模拟抽卡] 读取卡池数据失败: {error}")
            self._loaded = True
            return

        self._data = _parse_data(raw)
        if isinstance(raw, dict):
            pools_raw = raw.get("pools")
            if isinstance(pools_raw, dict):
                for pool_id, pool_raw in pools_raw.items():
                    pool = _parse_pool(str(pool_id), pool_raw)
                    if pool is not None:
                        self._pools[str(pool_id)] = pool
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
        self._load()

    def find_icon_path(self, url: str) -> Path | None:
        """按图标 URL 查本地缓存文件（文件名 = md5(url) + 扩展名）"""
        if not url.startswith("http"):
            return None
        digest = hashlib.md5(url.encode()).hexdigest()
        subdirs = ("角色", "角色碎片", "武器", "辅助机", "材料")
        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            filename = digest + ext
            path = ICON_DIR / filename
            if path.exists():
                return path
            for sub in subdirs:
                sub_path = ICON_DIR / sub / filename
                if sub_path.exists():
                    return sub_path
        return None


pool_manager = PoolManager()
