"""战双模拟抽卡 - 帮助图生成（玻璃拟态风格自研模板）"""

import json
from base64 import b64encode
from pathlib import Path

from gsuid_core.sv import get_plugin_available_prefix

from ..version import PGRGachaSim_version
from ..draw_gacha_result import _render_html_local, gacha_templates

HELP_DATA = Path(__file__).parent / "help.json"
ICON_FILE = Path(__file__).parent.parent.parent / "ICON.png"

# 列分配：左列为三类研发，右列为卡池管理与主人功能
_COLS = (("构造体研发", "武器研发", "辅助机研发"), ("卡池管理", "bot主人功能"))
_COL_LEFT = (48, 720)
_TOP0 = 140
_GROUP_HEAD = 76  # 组卡片标题区高度
_ROW_H = 56  # 指令行占位（卡 48 + 间距 8）
_GROUP_PAD = 16  # 组卡片底部内边距
_GROUP_GAP = 20

_GROUP_COLORS = {
    "构造体研发": "linear-gradient(135deg, #4E8DFF, #7B61FF)",
    "武器研发": "linear-gradient(135deg, #B16CFF, #FF6FD8)",
    "辅助机研发": "linear-gradient(135deg, #2ED3B7, #4E8DFF)",
    "卡池管理": "linear-gradient(135deg, #FFB54E, #FF7A59)",
    "bot主人功能": "linear-gradient(135deg, #FF6B81, #C44EFF)",
}
_ADMIN_COLOR = "linear-gradient(135deg, #E8B45A, #D98A3D)"
_DEFAULT_COLOR = "linear-gradient(135deg, #6B7A99, #4A5570)"


def get_help_data() -> dict:
    with open(HELP_DATA, "r", encoding="utf-8") as file:
        raw = json.load(file)
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items()}
    return {}


def _get_icon_base64() -> str:
    if not ICON_FILE.exists():
        return ""
    return "data:image/png;base64," + b64encode(ICON_FILE.read_bytes()).decode()


def _text_width(text: str, cjk: int, half: int) -> int:
    """粗估文本像素宽度"""
    width = 0
    for ch in text:
        width += cjk if ord(ch) > 0x2E7F else half
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
            color = _GROUP_COLORS.get(name, _DEFAULT_COLOR)
            items: list[dict] = []
            for entry in group.get("data", []):
                if not isinstance(entry, dict):
                    continue
                admin = bool(entry.get("need_admin"))
                if admin and pm != 0:
                    continue
                cmd_name = str(entry.get("name", ""))
                eg = str(entry.get("eg", ""))
                name_w = _text_width(prefix + cmd_name, 15, 9) + 28
                eg_w = _text_width(prefix + eg, 13, 7) + 24
                desc_left = 12 + name_w + 14
                eg_left = 600 - 12 - eg_w
                items.append(
                    {
                        "top": _GROUP_HEAD + len(items) * _ROW_H,
                        "name": cmd_name,
                        "desc": str(entry.get("desc", "")),
                        "eg": eg,
                        "color": _ADMIN_COLOR if admin else color,
                        "name_w": name_w,
                        "desc_left": desc_left,
                        "desc_w": max(80, eg_left - desc_left - 12),
                        "eg_left": eg_left,
                        "eg_w": eg_w,
                    }
                )
            if not items:
                continue
            card_height = _GROUP_HEAD + len(items) * _ROW_H + _GROUP_PAD
            groups.append(
                {
                    "title": name,
                    "desc": str(group.get("desc", "")),
                    "top": y,
                    "height": card_height,
                    "color": color,
                    "cmds": items,
                }
            )
            y += card_height + _GROUP_GAP
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
        "icon": _get_icon_base64(),
        "total_height": foot_top + 44,
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
