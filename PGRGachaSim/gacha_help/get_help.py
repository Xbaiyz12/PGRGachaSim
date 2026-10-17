"""战双模拟抽卡 - 帮助图生成"""

import json
from typing import Dict
from pathlib import Path

from PIL import Image

from gsuid_core.sv import get_plugin_available_prefix
from gsuid_core.help.model import PluginHelp
from gsuid_core.help.draw_new_plugin_help import get_new_help

from ..version import PGRGachaSim_version

HELP_DATA = Path(__file__).parent / "help.json"


def get_help_data() -> Dict[str, PluginHelp]:
    with open(HELP_DATA, "r", encoding="utf-8") as file:
        return json.load(file)


async def get_help(pm: int = 6):
    icon = Image.new("RGBA", (128, 128), (14, 24, 46, 255))
    return await get_new_help(
        plugin_name="PGRGachaSim",
        plugin_info={f"v{PGRGachaSim_version}": ""},
        plugin_icon=icon,
        plugin_help=get_help_data(),
        plugin_prefix=get_plugin_available_prefix("PGRGachaSim"),
        help_mode="dark",
        banner_sub_text="指挥官，开始研发吧",
        enable_cache=False,
        pm=pm,
        column=4,
    )
