"""战双模拟抽卡 - 帮助图生成（gscore 新版帮助样式，与 PGRUID 同款）"""

import json
from copy import deepcopy
from pathlib import Path
from typing import Dict

from PIL import Image

from gsuid_core.help.model import PluginHelp
from gsuid_core.help.draw_new_plugin_help import get_new_help
from gsuid_core.sv import get_plugin_available_prefix

from ..version import PGRGachaSim_version

ICON = Path(__file__).parent.parent.parent / "ICON.png"
HELP_DATA = Path(__file__).parent / "help.json"
ICON_PATH = Path(__file__).parent / "icon_path"

# 复用 PGRUID 的 banner 与背景素材（存在时），保持与战双插件一致的风格
PGRUID_HELP_PATH = (
    Path(__file__).parent.parent.parent.parent / "PGRUID" / "PGRUID" / "pgr_help"
)


def get_help_data() -> Dict[str, PluginHelp]:
    """读取 help.json"""
    with open(HELP_DATA, "r", encoding="utf-8") as file:
        return json.load(file)


async def get_help(pm: int = 6):
    """生成帮助图，need_admin 指令仅主人（pm=0）可见"""
    safe_pm = pm if isinstance(pm, int) else 6

    # 按权限过滤管理员专属指令（核心渲染器只支持组级 pm 过滤，指令级在此处理）
    help_data = deepcopy(get_help_data())
    for group in list(help_data.values()):
        if not isinstance(group, dict):
            continue
        group["data"] = [
            entry
            for entry in group.get("data", [])
            if isinstance(entry, dict)
            and (safe_pm == 0 or not entry.get("need_admin"))
        ]
    help_data = {
        name: group
        for name, group in help_data.items()
        if isinstance(group, dict) and group.get("data")
    }

    if ICON.exists():
        plugin_icon = Image.open(ICON).convert("RGBA")
    else:
        plugin_icon = Image.new("RGBA", (100, 100), (70, 130, 180, 255))

    kwargs = dict(
        plugin_name="PGRGachaSim",
        plugin_info={f"v{PGRGachaSim_version}": ""},
        plugin_icon=plugin_icon,
        plugin_help=help_data,
        plugin_prefix=get_plugin_available_prefix("PGRGachaSim"),
        help_mode="dark",
        banner_sub_text="指挥官，试试你的运气吧！",
        enable_cache=False,
        pm=safe_pm,
        column=3,
    )

    if ICON_PATH.exists():
        kwargs["icon_path"] = ICON_PATH

    banner = PGRUID_HELP_PATH / "banner.jpg"
    bg = PGRUID_HELP_PATH / "bg.jpg"
    if banner.exists():
        kwargs["banner_bg"] = Image.open(banner).convert("RGBA")
    if bg.exists():
        kwargs["help_bg"] = Image.open(bg).convert("RGBA")

    return await get_new_help(**kwargs)
