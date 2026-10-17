"""战双模拟抽卡 - 从官方公告更新当期卡池"""

import re
import json
import uuid

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
        content = node.get("content")
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


async def fetch_current_up() -> list[str]:
    """拉取最新研发公告，返回当期 UP 角色名（可能为空）"""
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            ANN_LIST_URL,
            headers=_headers(),
            data={"gameId": 2, "pageSize": 50},
        )
        raw = resp.json()
        items = (raw.get("data") or {}).get("list") or []
        candidates = [
            item
            for item in items
            if isinstance(item, dict) and any(key in str(item.get("postTitle") or "") for key in ("主题研发", "主题UP"))
        ]
        for item in candidates:
            post_id = item.get("postId")
            if not post_id:
                continue
            detail = await client.post(
                ANN_DETAIL_URL,
                headers=_headers(),
                data={"isOnlyPublisher": 1, "postId": str(post_id), "showOrderType": 2},
            )
            post = (detail.json().get("data") or {}).get("postDetail") or {}
            text = _content_text(post.get("postContent"))
            names = _extract_names(text)
            if names:
                return names
    return []


def _read_data() -> dict:
    path = USER_DATA_FILE if USER_DATA_FILE.exists() else BUILTIN_DATA_FILE
    with open(path, "r", encoding="utf-8") as file:
        raw = json.load(file)
    if not isinstance(raw, dict):
        raise ValueError("gacha_data.json 结构无效")
    return raw


def _write_user_data(raw: dict) -> None:
    USER_DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(USER_DATA_FILE, "w", encoding="utf-8") as file:
        json.dump(raw, file, ensure_ascii=False, indent=2)


async def update_current_pools() -> list[str]:
    """拉公告更新当期 UP，返回实际写入的 UP 角色名"""
    raw = _read_data()
    s_items = raw.get("characters", {}).get("S", [])
    s_names = [item.get("name", "") for item in s_items if isinstance(item, dict) and item.get("name")]
    up_names = await fetch_current_up()
    matched: list[str] = []
    for name in up_names:
        if name in s_names and name not in matched:
            matched.append(name)
    if not matched:
        return []
    standard_s = [name for name in s_names if name not in matched]
    raw["standard_s"] = standard_s
    pools = raw.get("pools", {})
    for pool_id in ("char_theme", "char_fate"):
        pool = pools.get(pool_id)
        if isinstance(pool, dict):
            pool["up_s"] = matched
    _write_user_data(raw)
    pool_manager.reload()
    logger.info(f"[战双·模拟抽卡] 卡池已更新，当期 UP: {'、'.join(matched)}")
    return matched
