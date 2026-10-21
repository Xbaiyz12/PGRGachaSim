"""战双模拟抽卡 - 从官方公告更新当期卡池"""

import os
import re
import json
import uuid
from pathlib import Path

import httpx

from gsuid_core.logger import logger

from .pool_manager import USER_DATA_FILE, BUILTIN_DATA_FILE, pool_manager

KURO_VERSION = "3.1.3"
CONTENT_TYPE = "application/x-www-form-urlencoded; charset=utf-8"
IOS_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko)  KuroGameBox/3.1.3"
)
ANN_LIST_URL = "https://api.kurobbs.com/forum/companyEvent/findEventList"
ANN_DETAIL_URL = "https://api.kurobbs.com/forum/getPostDetail"


def _headers() -> dict[str, str]:
    return {
        "source": "h5",
        "Content-Type": CONTENT_TYPE,
        "User-Agent": IOS_UA,
        "devCode": str(uuid.uuid4()),
        "version": KURO_VERSION,
    }


def _walk_text(node: object, out: list[str]) -> None:
    if isinstance(node, dict):
        if "content" in node:
            content = node["content"]
            if isinstance(content, str) and content:
                out.append(content)
        for value in node.values():
            if isinstance(value, (dict, list)):
                _walk_text(value, out)
    elif isinstance(node, list):
        for item in node:
            _walk_text(item, out)


def _content_text(content: object) -> str:
    out: list[str] = []
    _walk_text(content, out)
    return "".join(out)


def _normalize(name: str) -> str:
    return name.replace("•", "·").replace("・", "·").strip()


def _extract_names(text: str) -> list[str]:
    names = [_normalize(name) for name in re.findall(r"「([^」]+)」", text)]
    if not names:
        names = [_normalize(name) for name in re.findall(r"【([^】]+)】", text)]
    return names


async def fetch_current_up(valid_names: set[str] | None = None) -> list[str]:
    """拉取最新研发公告，返回当期 UP 角色名（可能为空）。
    传入 valid_names 时会跳过名字不在集合中的候选公告，避免被无关公告截胡。
    """
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                ANN_LIST_URL,
                headers=_headers(),
                data={"gameId": 2, "pageSize": 50},
            )
            raw = resp.json()
            if not isinstance(raw, dict) or "data" not in raw:
                return []
            data_node = raw["data"]
            if not isinstance(data_node, dict) or "list" not in data_node:
                return []
            items = data_node["list"]
            if not isinstance(items, list):
                return []

            candidates: list[dict[str, object]] = []
            for item in items:
                if isinstance(item, dict) and "postTitle" in item:
                    title_val = str(item["postTitle"])
                    if "主题研发" in title_val or "主题UP" in title_val:
                        candidates.append(item)

            for item in candidates:
                if "postId" not in item:
                    continue
                post_id = item["postId"]
                if not post_id:
                    continue
                detail = await client.post(
                    ANN_DETAIL_URL,
                    headers=_headers(),
                    data={"isOnlyPublisher": 1, "postId": str(post_id), "showOrderType": 2},
                )
                detail_raw = detail.json()
                if not isinstance(detail_raw, dict) or "data" not in detail_raw:
                    continue
                detail_data = detail_raw["data"]
                if not isinstance(detail_data, dict) or "postDetail" not in detail_data:
                    continue
                post = detail_data["postDetail"]
                if not isinstance(post, dict) or "postContent" not in post:
                    continue
                text = _content_text(post["postContent"])
                names = _extract_names(text)
                if not names:
                    continue
                if valid_names is not None and not any(name in valid_names for name in names):
                    continue
                return names
    except (httpx.HTTPError, OSError, json.JSONDecodeError) as error:
        logger.warning(f"[战双·模拟抽卡] 拉取公告失败: {error}")
    return []


def _read_data() -> dict[str, object]:
    path = USER_DATA_FILE if USER_DATA_FILE.exists() else BUILTIN_DATA_FILE
    with open(path, "r", encoding="utf-8") as file:
        raw = json.load(file)
    if not isinstance(raw, dict):
        raise ValueError("gacha_data.json 结构无效")
    return {str(k): v for k, v in raw.items()}


def _write_user_data(raw: dict[str, object]) -> None:
    USER_DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = Path(f"{USER_DATA_FILE}.tmp.{uuid.uuid4().hex}")
    with open(temp_file, "w", encoding="utf-8") as file:
        json.dump(raw, file, ensure_ascii=False, indent=2)
    os.replace(temp_file, USER_DATA_FILE)


async def update_current_pools() -> list[str]:
    """拉公告更新当期 UP，返回实际写入的 UP 角色名"""
    raw = _read_data()
    chars_node = raw["characters"] if "characters" in raw and isinstance(raw["characters"], dict) else {}
    s_items = chars_node["S"] if "S" in chars_node and isinstance(chars_node["S"], list) else []
    s_names: list[str] = []
    for item in s_items:
        if isinstance(item, dict) and "name" in item:
            name_val = str(item["name"])
            if name_val:
                s_names.append(name_val)

    up_names = await fetch_current_up(set(s_names))
    matched: list[str] = []
    for name in up_names:
        if name in s_names and name not in matched:
            matched.append(name)
    if not matched:
        return []

    standard_s = [name for name in s_names if name not in matched]
    raw["standard_s"] = standard_s
    pools = raw["pools"] if "pools" in raw and isinstance(raw["pools"], dict) else {}
    for pool_id in ("char_theme", "char_fate"):
        if pool_id in pools:
            pool = pools[pool_id]
            if isinstance(pool, dict):
                pool["up_s"] = matched

    _write_user_data(raw)
    pool_manager.reload()
    logger.info(f"[战双·模拟抽卡] 卡池已更新，当期 UP: {'、'.join(matched)}")
    return matched
