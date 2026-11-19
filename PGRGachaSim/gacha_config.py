"""战双模拟抽卡 - 配置"""

from typing import Dict

from gsuid_core.data_store import get_res_path
from gsuid_core.utils.plugins_config.models import GSC, GsIntConfig, GsStrConfig, GsBoolConfig
from gsuid_core.utils.plugins_config.gs_config import StringConfig

GACHA_SIM_MAIN_PATH = get_res_path() / "PGRGachaSim"
GACHA_SIM_MAIN_PATH.mkdir(parents=True, exist_ok=True)
GACHA_SIM_CONFIG_PATH = GACHA_SIM_MAIN_PATH / "gacha_config.json"

GACHA_SIM_CONFIG_DEFAULT: Dict[str, GSC] = {
    "PGRGachaEnabled": GsBoolConfig(
        title="模拟抽卡总开关",
        desc="关闭后所有模拟抽卡命令不可用",
        data=True,
    ),
    "PGRGachaDailyLimit": GsIntConfig(
        title="每日抽卡次数上限",
        desc="每个用户每天每种卡池类型的模拟抽卡次数上限，0为无限制",
        data=300,
        max_value=9999,
    ),
    "PGRGachaMasterUnlimited": GsBoolConfig(
        title="主人无限抽取",
        desc="开启后，主人（pm=0）不受每日抽卡次数限制",
        data=True,
    ),
    "PGRGachaTextFallback": GsBoolConfig(
        title="渲染失败时使用文本",
        desc="图片渲染失败时，自动回退为纯文本发送结果",
        data=True,
    ),
    "PGRGachaRemoteRenderEnable": GsBoolConfig(
        title="外置渲染开关",
        desc="开启后将使用外置渲染服务，失败时自动回退到本地渲染",
        data=False,
    ),
    "PGRGachaRemoteRenderUrl": GsStrConfig(
        title="外置渲染地址",
        desc="外置渲染服务的API地址，例如：http://127.0.0.1:3000/render",
        data="http://127.0.0.1:3000/render",
    ),
    "PGRGachaEnableBailian": GsBoolConfig(
        title="百连功能开关",
        desc="开启后可用「抽卡百连」，连续 10 次十连（共 100 抽）",
        data=False,
    ),
    "PGRGachaBailianMerge": GsBoolConfig(
        title="百连合并转发",
        desc="仅第三方协议端可用：开启后发送 10 张十连图并合并转发；官方机器人请保持关闭，只发单张汇总图",
        data=False,
    ),
}

GachaSimConfig = StringConfig(
    "战双模拟抽卡",
    GACHA_SIM_CONFIG_PATH,
    GACHA_SIM_CONFIG_DEFAULT,
)
