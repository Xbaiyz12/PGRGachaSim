"""战双模拟抽卡 - 帮助图生成（战双风格自研模板）"""

import json
from pathlib import Path

from gsuid_core.sv import get_plugin_available_prefix

from ..version import PGRGachaSim_version
from ..draw_gacha_result import _get_bg_base64, _render_html_local, gacha_templates

HELP_DATA = Path(__file__).parent / "help.json"

# 列分配：左列为三类研发，右列为卡池管理与主人功能
_COLS = (("构造体研发", "武器研发", "辅助机研发"), ("卡池管理", "bot主人功能"))
_COL_LEFT = (48, 720)
_TOP0 = 130
_GROUP_H = 64
_ROW_H = 72
_GROUP_GAP = 20


def get_help_data() -> dict:
    with open(HELP_DATA, "r", encoding="utf-8") as file:
        raw = json.load(file)
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items()}
    return {}


def _text_width(text: str) -> int:
    """粗估文本像素宽度：全角 22px，半角 12px（用于管理员徽章定位）"""
    width = 0
    for ch in text:
        width += 22 if ord(ch) > 0x2E7F else 12
    return width


def _build_columns(help_data: dict, prefix: str, pm: int) -> tuple[list[dict], int]:
    columns: list[dict] = []
    max_height = 0
    for col_index, group_names in enumerate(_COLS):
        y = _TOP0
        groups: list[dict] = []
        for name in group_names:
            group = help_data.get(name)
            if not isinstance(group, dict):
                continue
            items: list[dict] = []
            for entry in group.get("data", []):
                if not isinstance(entry, dict):
                    continue
                if entry.get("need_admin") and pm != 0:
                    continue
                item = {
                    "top": y + _GROUP_H + len(items) * _ROW_H,
                    "name": str(entry.get("name", "")),
                    "desc": str(entry.get("desc", "")),
                    "eg": str(entry.get("eg", "")),
                    "admin": bool(entry.get("need_admin")),
                }
                item["admin_left"] = 20 + _text_width(prefix + item["name"]) + 14
                items.append(item)
            if not items:
                continue
            groups.append(
                {
                    "title": name,
                    "desc": str(group.get("desc", "")),
                    "top": y,
                    "cmds": items,
                }
            )
            y += _GROUP_H + len(items) * _ROW_H + _GROUP_GAP
        height = y - _GROUP_GAP - _TOP0 if groups else 0
        max_height = max(max_height, height)
        columns.append({"left": _COL_LEFT[col_index], "groups": groups})
    return columns, max_height


async def get_help(pm: int = 6) -> bytes | str | None:
    safe_pm = pm if isinstance(pm, int) else 6
    prefix = get_plugin_available_prefix("PGRGachaSim")
    columns, max_height = _build_columns(get_help_data(), prefix, safe_pm)
    foot_top = _TOP0 + max_height + 24
    context = {
        "columns": columns,
        "prefix": prefix,
        "version": PGRGachaSim_version,
        "background": _get_bg_base64(),
        "total_height": foot_top + 48,
        "foot_top": foot_top,
    }
    image = await _render_html_local(gacha_templates.get_template("gacha_help.html"), context)
    if image is not None:
        return image
    # 渲染失败时回退纯文本帮助
    lines = ["【战双模拟抽卡 · 指令帮助】"]
    for col in columns:
        for group in col["groups"]:
            lines.append(f"\n◆ {group['title']}")
            for item in group["cmds"]:
                lines.append(f"  {prefix}{item['name']}：{item['desc']}")
    return "\n".join(lines)
