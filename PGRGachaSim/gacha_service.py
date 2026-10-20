"""战双模拟抽卡 - 核心抽卡逻辑（参照 BWIKI 新抽卡模拟器）"""

import random
from typing import TypedDict


class GachaItem(TypedDict, total=False):
    name: str
    mainIcon: str
    shardsIcon: str
    figure: str


class GachaResult(TypedDict):
    name: str
    level: int
    pillar_level: int
    category: str
    icon: str
    figure: str
    amount: int
    is_up: bool
    pity_count: int
    shards_icon: str


class PityData(TypedDict):
    current_count: int
    sub_count: int
    guaranteed_up: bool
    total_count: int


class PoolData(TypedDict, total=False):
    id: str
    name: str
    type: str
    target: str
    total: int
    weights: dict[str, float]
    up_s: list[str]
    up_a: list[str]
    up_w6: list[str]
    up_rate_s: float
    up_rate_a: float
    up_rate_w6: float


class VersionData(TypedDict, total=False):
    name: str
    icon: str
    roles: list[str]
    role: str
    weapon: str
    support: str


class GachaData(TypedDict):
    characters: dict[str, list[GachaItem]]
    weapons: dict[str, list[GachaItem]]
    materials: dict[str, list[GachaItem]]
    support: dict[str, list[GachaItem]]
    versions: list[VersionData]
    standard_s: list[str]


def empty_pity() -> PityData:
    return PityData(current_count=0, sub_count=0, guaranteed_up=False, total_count=0)


def _weighted_choice(weights: list[tuple[str, float]]) -> str:
    if not weights:
        return ""
    total = sum(weight for _, weight in weights)
    if total <= 0:
        return weights[0][0]
    roll = random.random() * total
    upto = 0.0
    for name, weight in weights:
        upto += weight
        if roll <= upto:
            return name
    return weights[-1][0]


def _item_str(item: GachaItem, key: str) -> str:
    if key in item:
        val = item[key]
        if isinstance(val, str):
            return val
    return ""


class GachaService:
    """战双抽卡核心服务"""

    def _pick(self, items: list[GachaItem]) -> GachaItem | None:
        return random.choice(items) if items else None

    def _make_result(
        self,
        item: GachaItem | None,
        level: int,
        category: str,
        is_up: bool,
        pity_count: int,
        use_shards: bool = False,
        amount: int = 1,
        pillar_level: int = 0,
    ) -> GachaResult:
        if item is None:
            return GachaResult(
                name="未知",
                level=level,
                pillar_level=pillar_level if pillar_level > 0 else level,
                category=category,
                icon="",
                figure="",
                amount=amount,
                is_up=is_up,
                pity_count=pity_count,
                shards_icon="",
            )
        name = _item_str(item, "name") or "未知"
        shards_icon = _item_str(item, "shardsIcon")
        main_icon = _item_str(item, "mainIcon")
        figure = "" if category == "fragment" else _item_str(item, "figure")

        if use_shards:
            icon = shards_icon or main_icon
        else:
            icon = main_icon or shards_icon

        return GachaResult(
            name=name,
            level=level,
            pillar_level=pillar_level if pillar_level > 0 else level,
            category=category,
            icon=icon,
            figure=figure,
            amount=amount,
            is_up=is_up,
            pity_count=pity_count,
            shards_icon=shards_icon,
        )

    def _pick_up(
        self,
        up_items: list[GachaItem],
        fallback_items: list[GachaItem],
        up_rate: float,
        guaranteed_up: bool,
    ) -> tuple[GachaItem | None, bool, bool]:
        """按概率抽取UP或常驻物品，返回 (item, is_up, next_guaranteed_up)"""
        if up_items and (guaranteed_up or random.random() * 100.0 < up_rate):
            return random.choice(up_items), True, False
        chosen = random.choice(fallback_items) if fallback_items else None
        # up_items 为空（未配置 UP）时不清除既有大保底
        return chosen, False, guaranteed_up or bool(up_items)

    def perform_draw(
        self,
        pool: PoolData,
        data: GachaData,
        pity: PityData,
        count: int = 10,
    ) -> list[GachaResult]:
        pool_type = pool["type"] if "type" in pool else ""
        if pool_type.startswith("weapon"):
            results = self._draw_weapon(pool, data, pity, count)
        elif pool_type == "support_unit":
            results = self._draw_support(pool, data, pity, count)
        else:
            results = self._draw_char(pool, data, pity, count)
        pity["total_count"] += count
        return results

    def _draw_char(
        self,
        pool: PoolData,
        data: GachaData,
        pity: PityData,
        count: int,
    ) -> list[GachaResult]:
        results: list[GachaResult] = []
        characters = data["characters"]
        materials = data["materials"]
        weights = pool["weights"] if "weights" in pool else {}
        current = pity["current_count"]
        sub = pity["sub_count"]
        guaranteed_up = pity["guaranteed_up"]

        a_list = characters["A"] if "A" in characters else []
        b_list = characters["B"] if "B" in characters else []
        s_list = characters["S"] if "S" in characters else []
        a_len = len(a_list)
        b_len = len(b_list)
        ab_total = weights["AB"] if "AB" in weights else 0.0
        a_weight = ab_total * a_len / (a_len + b_len) if (a_len + b_len) > 0 else 0.0
        b_weight = ab_total * b_len / (a_len + b_len) if (a_len + b_len) > 0 else 0.0

        base_s_weight = weights["S"] if "S" in weights else 0.5
        types = [
            ("S", base_s_weight),
            ("A", a_weight),
            ("B", b_weight),
            ("fragment", weights["fragment"] if "fragment" in weights else 0.0),
            ("memory", weights["memory"] if "memory" in weights else 0.0),
            ("overclock", weights["overclock"] if "overclock" in weights else 0.0),
            ("exp", weights["exp"] if "exp" in weights else 0.0),
            ("cogs", weights["cogs"] if "cogs" in weights else 0.0),
        ]

        pool_type = pool["type"] if "type" in pool else ""
        is_fate = pool_type == "char_fate"
        is_standard = pool_type == "char_standard"
        pity_s = pool["total"] if "total" in pool else (100 if is_fate else 60)
        pity_a = 10
        up_rate_s = pool["up_rate_s"] if "up_rate_s" in pool else 100.0
        up_rate_a = pool["up_rate_a"] if "up_rate_a" in pool else 80.0
        up_s = pool["up_s"] if "up_s" in pool else []
        up_a = pool["up_a"] if "up_a" in pool else []

        standard_names = set(data["standard_s"]) if "standard_s" in data else set()
        standard_items = [item for item in s_list if _item_str(item, "name") in standard_names] or s_list
        up_items = [item for item in s_list if _item_str(item, "name") in up_s]
        a_up_items = [item for item in a_list if _item_str(item, "name") in up_a]

        for _ in range(count):
            current += 1
            sub += 1

            # 命运池 80~100 抽动态上调 S 级综合概率
            if is_fate:
                if current >= 80:
                    ramp_rate = base_s_weight + (current - 80) * 4.95
                    types[0] = ("S", min(100.0, ramp_rate))
                else:
                    types[0] = ("S", base_s_weight)

            # S级硬保底检查
            if current >= pity_s:
                if is_standard:
                    item = self._pick(standard_items)
                    is_up = False
                else:
                    item, is_up, guaranteed_up = self._pick_up(up_items, standard_items, up_rate_s, guaranteed_up)
                results.append(self._make_result(item, 6, "character", is_up, current, pillar_level=6))
                current = 0
                sub = 0
                continue

            prize_type = _weighted_choice(types)

            # 自然抽中 S 级
            if prize_type == "S":
                if is_standard:
                    item = self._pick(standard_items)
                    is_up = False
                else:
                    item, is_up, guaranteed_up = self._pick_up(up_items, standard_items, up_rate_s, guaranteed_up)
                results.append(self._make_result(item, 6, "character", is_up, current, pillar_level=6))
                current = 0
                sub = 0
                continue

            # 未出S时检查 A 级次级保底
            if sub >= pity_a or prize_type == "A":
                item, is_up, _ = self._pick_up(a_up_items, a_list, up_rate_a, False)
                results.append(self._make_result(item, 5, "character", is_up, sub, pillar_level=5))
                sub = 0
                continue

            if prize_type == "B":
                item = self._pick(b_list)
                results.append(self._make_result(item, 4, "character", False, 0, pillar_level=4))
                # B 级角色不重置 A 级保底 sub 计数
            elif prize_type == "fragment":
                s_shards = [it for it in s_list if _item_str(it, "shardsIcon")]
                a_shards = [it for it in a_list if _item_str(it, "shardsIcon")]
                b_shards = [it for it in b_list if _item_str(it, "shardsIcon")]
                roll_val = random.random()
                if roll_val < 0.2 and s_shards:
                    item = self._pick(s_shards)
                elif roll_val < 0.6 and a_shards:
                    item = self._pick(a_shards)
                else:
                    item = self._pick(b_shards or a_shards or s_list)
                res = self._make_result(item, 4, "fragment", False, 0, use_shards=True, amount=2, pillar_level=4)
                res["figure"] = ""
                res["name"] = f"{res['name']}碎片×2"
                results.append(res)
            elif prize_type == "memory":
                mem_list = materials["memories4"] if "memories4" in materials else []
                item = self._pick(mem_list)
                results.append(self._make_result(item, 4, "material", False, 0, pillar_level=4))
            elif prize_type == "overclock":
                oc_list = materials["overClock"] if "overClock" in materials else []
                item = self._pick(oc_list)
                lvl = 3 if item is not None and "初" in _item_str(item, "name") else 4
                results.append(self._make_result(item, lvl, "material", False, 0, pillar_level=lvl))
            elif prize_type == "exp":
                exp_list = materials["expPods"] if "expPods" in materials else []
                item = self._pick(exp_list)
                results.append(self._make_result(item, 4, "material", False, 0, pillar_level=4))
            else:
                cogs_list = materials["cogs"] if "cogs" in materials else []
                item = self._pick(cogs_list)
                results.append(self._make_result(item, 4, "material", False, 0, pillar_level=4))

        pity["current_count"] = current
        pity["sub_count"] = sub
        pity["guaranteed_up"] = guaranteed_up
        return results

    def _draw_support(
        self,
        pool: PoolData,
        data: GachaData,
        pity: PityData,
        count: int,
    ) -> list[GachaResult]:
        results: list[GachaResult] = []
        support = data["support"]
        materials = data["materials"]
        weights = pool["weights"] if "weights" in pool else {}
        current = pity["current_count"]
        sub = pity["sub_count"]
        guaranteed_up = pity["guaranteed_up"]

        types = [
            ("S", weights["S"] if "S" in weights else 0.0),
            ("A", weights["A"] if "A" in weights else 0.0),
            ("exp", weights["exp"] if "exp" in weights else 0.0),
            ("overclock", weights["overclock"] if "overclock" in weights else 0.0),
            ("skill", weights["skill"] if "skill" in weights else 0.0),
            ("fragment", weights["fragment"] if "fragment" in weights else 0.0),
            ("cogs", weights["cogs"] if "cogs" in weights else 0.0),
        ]
        pity_s = pool["total"] if "total" in pool else 20
        pity_a = 10
        s_items = support["S"] if "S" in support else []
        a_items = support["A"] if "A" in support else []
        up_s = pool["up_s"] if "up_s" in pool else []
        up_a = pool["up_a"] if "up_a" in pool else []
        up_s_items = [entry for entry in s_items if _item_str(entry, "name") in up_s]
        up_a_items = [entry for entry in a_items if _item_str(entry, "name") in up_a]

        for _ in range(count):
            current += 1
            sub += 1

            # S级保底判定
            if current >= pity_s:
                item, is_up, guaranteed_up = self._pick_up(up_s_items, s_items, 80.0, guaranteed_up)
                results.append(self._make_result(item, 6, "support", is_up, current, pillar_level=6))
                current = 0
                sub = 0
                continue

            prize_type = _weighted_choice(types)

            # 自然出 S 级
            if prize_type == "S":
                item, is_up, guaranteed_up = self._pick_up(up_s_items, s_items, 80.0, guaranteed_up)
                results.append(self._make_result(item, 6, "support", is_up, current, pillar_level=6))
                current = 0
                sub = 0
                continue

            # 次级 A 级保底或自然出 A
            if sub >= pity_a or prize_type == "A":
                item, is_up, _ = self._pick_up(up_a_items, a_items, 80.0, False)
                results.append(self._make_result(item, 5, "support", is_up, sub, pillar_level=5))
                sub = 0
                continue

            if prize_type == "exp":
                exp_list = support["exp"] if "exp" in support else []
                item = self._pick(exp_list)
                lvl = 3 if item is not None and "小" in _item_str(item, "name") else 4
                results.append(self._make_result(item, lvl, "material", False, 0, pillar_level=lvl))
            elif prize_type == "overclock":
                oc_list = support["overClock"] if "overClock" in support else []
                item = self._pick(oc_list)
                lvl = 3 if item is not None and "小" in _item_str(item, "name") else 4
                results.append(self._make_result(item, lvl, "material", False, 0, pillar_level=lvl))
            elif prize_type == "skill":
                skill_list = support["skill"] if "skill" in support else []
                item = self._pick(skill_list)
                results.append(self._make_result(item, 4, "material", False, 0, pillar_level=4))
            elif prize_type == "fragment":
                s_shards = [it for it in s_items if _item_str(it, "shardsIcon")]
                a_shards = [it for it in a_items if _item_str(it, "shardsIcon")]
                if random.random() < 0.5 and s_shards:
                    item = self._pick(s_shards)
                else:
                    item = self._pick(a_shards or s_items)
                res = self._make_result(item, 4, "fragment", False, 0, use_shards=True, amount=2, pillar_level=4)
                res["figure"] = ""
                res["name"] = f"支援架构零件-{res['name']}"
                results.append(res)
            else:
                cogs_list = materials["cogs"] if "cogs" in materials else []
                item = self._pick(cogs_list)
                results.append(self._make_result(item, 4, "material", False, 0, pillar_level=4))

        pity["current_count"] = current
        pity["sub_count"] = sub
        pity["guaranteed_up"] = guaranteed_up
        return results

    def _draw_weapon(
        self,
        pool: PoolData,
        data: GachaData,
        pity: PityData,
        count: int,
    ) -> list[GachaResult]:
        results: list[GachaResult] = []
        weapons = data["weapons"]
        materials = data["materials"]
        weights = pool["weights"] if "weights" in pool else {}
        current = pity["current_count"]
        sub = pity["sub_count"]
        guaranteed_up = pity["guaranteed_up"]

        types = [
            ("6", weights["6"] if "6" in weights else 0.0),
            ("5", weights["5"] if "5" in weights else 0.0),
            ("4", weights["4"] if "4" in weights else 0.0),
            ("3", weights["3"] if "3" in weights else 0.0),
            ("overclock", weights["overclock"] if "overclock" in weights else 0.0),
            ("cogs", weights["cogs"] if "cogs" in weights else 0.0),
        ]
        pity_6 = pool["total"] if "total" in pool else 30
        pity_5 = 10
        up_rate_w6 = pool["up_rate_w6"] if "up_rate_w6" in pool else 80.0
        up_w6 = pool["up_w6"] if "up_w6" in pool else []
        w6_items = weapons["6"] if "6" in weapons else []
        w5_items = weapons["5"] if "5" in weapons else []
        w4_items = weapons["4"] if "4" in weapons else []
        w3_items = weapons["3"] if "3" in weapons else []
        w6_up_items = [item for item in w6_items if _item_str(item, "name") in up_w6]

        for _ in range(count):
            current += 1
            sub += 1

            # 6星保底检查
            if current >= pity_6:
                item, is_up, guaranteed_up = self._pick_up(w6_up_items, w6_items, up_rate_w6, guaranteed_up)
                results.append(self._make_result(item, 6, "weapon", is_up, current, pillar_level=6))
                current = 0
                sub = 0
                continue

            prize_type = _weighted_choice(types)

            # 自然抽中 6星
            if prize_type == "6":
                item, is_up, guaranteed_up = self._pick_up(w6_up_items, w6_items, up_rate_w6, guaranteed_up)
                results.append(self._make_result(item, 6, "weapon", is_up, current, pillar_level=6))
                current = 0
                sub = 0
                continue

            # 5星次级保底检查或自然抽中 5星
            if sub >= pity_5 or prize_type == "5":
                item = self._pick(w5_items)
                results.append(self._make_result(item, 5, "weapon", False, sub, pillar_level=5))
                sub = 0
                continue

            if prize_type == "4":
                item = self._pick(w4_items)
                results.append(self._make_result(item, 4, "weapon", False, 0, pillar_level=4))
            elif prize_type == "3":
                item = self._pick(w3_items)
                results.append(self._make_result(item, 3, "weapon", False, 0, pillar_level=3))
            elif prize_type == "overclock":
                oc_list = materials["overClock"] if "overClock" in materials else []
                item = self._pick(oc_list)
                results.append(self._make_result(item, 4, "material", False, 0, pillar_level=4))
            else:
                cogs_list = materials["cogs"] if "cogs" in materials else []
                item = self._pick(cogs_list)
                results.append(self._make_result(item, 4, "material", False, 0, pillar_level=4))

        pity["current_count"] = current
        pity["sub_count"] = sub
        pity["guaranteed_up"] = guaranteed_up
        return results


gacha_service = GachaService()
