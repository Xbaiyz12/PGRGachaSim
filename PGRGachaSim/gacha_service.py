"""战双模拟抽卡 - 核心抽卡逻辑（参照 BWIKI 新抽卡模拟器）"""

import random
from typing import TypedDict


class GachaItem(TypedDict, total=False):
    name: str
    mainIcon: str
    shardsIcon: str
    image: str
    figure: str


class GachaResult(TypedDict):
    name: str
    level: int
    category: str
    icon: str
    is_up: bool
    pity_count: int


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
    total = sum(weight for _, weight in weights)
    roll = random.random() * total
    upto = 0.0
    for name, weight in weights:
        upto += weight
        if roll <= upto:
            return name
    return weights[-1][0]


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
    ) -> GachaResult:
        if item is None:
            return GachaResult(
                name="未知",
                level=level,
                category=category,
                icon="",
                is_up=is_up,
                pity_count=pity_count,
            )
        icon = ""
        if use_shards:
            icon = item.get("shardsIcon") or item.get("mainIcon") or ""
        else:
            icon = item.get("mainIcon") or item.get("shardsIcon") or ""
        return GachaResult(
            name=item.get("name", "未知"),
            level=level,
            category=category,
            icon=icon,
            is_up=is_up,
            pity_count=pity_count,
        )

    def _pick_up(
        self,
        up_items: list[GachaItem],
        fallback_items: list[GachaItem],
        up_rate: float,
        guaranteed_up: bool,
    ) -> tuple[GachaItem | None, bool, bool]:
        """返回 (物品, 是否UP, 新的guaranteed_up)；歪卡只从 fallback（常驻）里出"""
        if up_items and (guaranteed_up or random.random() * 100 < up_rate):
            return random.choice(up_items), True, False
        return (random.choice(fallback_items) if fallback_items else None), False, bool(up_items and not guaranteed_up)

    def perform_draw(
        self,
        pool: PoolData,
        data: GachaData,
        pity: PityData,
        count: int = 10,
    ) -> list[GachaResult]:
        pool_type = str(pool.get("type", ""))
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
        weights = pool.get("weights", {})
        current = pity["current_count"]
        sub = pity["sub_count"]
        guaranteed_up = pity["guaranteed_up"]
        a_len = len(characters.get("A", []))
        b_len = len(characters.get("B", []))
        ab_total = float(weights.get("AB", 0))
        a_weight = ab_total * a_len / (a_len + b_len) if (a_len + b_len) else 0.0
        b_weight = ab_total * b_len / (a_len + b_len) if (a_len + b_len) else 0.0
        types = [
            ("S", float(weights.get("S", 0))),
            ("A", a_weight),
            ("B", b_weight),
            ("fragment", float(weights.get("fragment", 0))),
            ("memory", float(weights.get("memory", 0))),
            ("overclock", float(weights.get("overclock", 0))),
            ("exp", float(weights.get("exp", 0))),
            ("cogs", float(weights.get("cogs", 0))),
        ]
        is_fate = pool.get("type") == "char_fate"
        base_s = float(weights.get("S", 0.5))
        pity_s = int(pool.get("total", 60))
        pity_a = 10
        up_rate_s = float(pool.get("up_rate_s", 0))
        up_rate_a = float(pool.get("up_rate_a", 80))
        up_s = pool.get("up_s", [])
        up_a = pool.get("up_a", [])
        s_items = characters.get("S", [])
        standard_names = data.get("standard_s", [])
        standard_items = [item for item in s_items if item.get("name") in standard_names] or s_items
        up_items = [item for item in s_items if item.get("name") in up_s]
        a_items = characters.get("A", [])
        a_up_items = [item for item in a_items if item.get("name") in up_a]
        is_standard = pool.get("type") == "char_standard"

        for _ in range(count):
            current += 1
            sub += 1
            if is_fate:
                types[0] = ("S", 1.9 if current >= 80 else base_s)
            if current >= pity_s:
                if is_standard:
                    item = self._pick(standard_items)
                    is_up = False
                else:
                    item, is_up, guaranteed_up = self._pick_up(up_items, standard_items, up_rate_s, guaranteed_up)
                results.append(self._make_result(item, 6, "character", is_up, current))
                current = 0
                sub = 0
                continue
            if sub >= pity_a:
                item, is_up, _ = self._pick_up(a_up_items, a_items, up_rate_a, False)
                results.append(self._make_result(item, 5, "character", is_up, sub))
                sub = 0
                continue

            prize_type = _weighted_choice(types)
            if prize_type == "S":
                if is_standard:
                    item = self._pick(standard_items)
                    is_up = False
                else:
                    item, is_up, guaranteed_up = self._pick_up(up_items, standard_items, up_rate_s, guaranteed_up)
                results.append(self._make_result(item, 6, "character", is_up, current))
                current = 0
                sub = 0
            elif prize_type == "A":
                item, is_up, _ = self._pick_up(a_up_items, a_items, up_rate_a, False)
                results.append(self._make_result(item, 5, "character", is_up, sub))
                sub = 0
            elif prize_type == "B":
                item = self._pick(characters.get("B", []))
                results.append(self._make_result(item, 5, "character", False, 0))
                sub = 0
            elif prize_type == "fragment":
                item = self._pick(characters.get("S", []) + characters.get("A", []) + characters.get("B", []))
                result = self._make_result(item, 4, "fragment", False, 0, use_shards=True)
                result["name"] = f"{result['name']}碎片×2"
                results.append(result)
            elif prize_type == "memory":
                item = self._pick(materials.get("memories4", []))
                results.append(self._make_result(item, 4, "material", False, 0))
            elif prize_type == "overclock":
                item = self._pick(materials.get("overClock", []))
                level = 3 if item is not None and "初" in item.get("name", "") else 4
                results.append(self._make_result(item, level, "material", False, 0))
            elif prize_type == "exp":
                item = self._pick(materials.get("expPods", []))
                results.append(self._make_result(item, 4, "material", False, 0))
            else:
                item = self._pick(materials.get("cogs", []))
                results.append(self._make_result(item, 4, "material", False, 0))

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
        weights = pool.get("weights", {})
        current = pity["current_count"]
        sub = pity["sub_count"]
        types = [
            ("S", float(weights.get("S", 0))),
            ("A", float(weights.get("A", 0))),
            ("exp", float(weights.get("exp", 0))),
            ("overclock", float(weights.get("overclock", 0))),
            ("skill", float(weights.get("skill", 0))),
            ("fragment", float(weights.get("fragment", 0))),
            ("cogs", float(weights.get("cogs", 0))),
        ]
        pity_s = int(pool.get("total", 20))
        pity_a = 10
        s_items = support.get("S", [])
        a_items = support.get("A", [])
        up_all = pool.get("up_s") or []
        up_s_items = [entry for entry in s_items if entry.get("name") in up_all]
        up_a_items = [entry for entry in a_items if entry.get("name") in up_all]

        for _ in range(count):
            current += 1
            sub += 1
            if current >= pity_s:
                item = self._pick(up_s_items) if up_s_items and random.random() < 0.8 else self._pick(s_items)
                results.append(self._make_result(item, 6, "support", False, current))
                current = 0
                sub = 0
                continue
            if sub >= pity_a:
                item = self._pick(up_a_items) if up_a_items and random.random() < 0.8 else self._pick(a_items)
                results.append(self._make_result(item, 5, "support", False, sub))
                sub = 0
                continue

            prize_type = _weighted_choice(types)
            if prize_type == "S":
                item = self._pick(up_s_items) if up_s_items and random.random() < 0.8 else self._pick(s_items)
                results.append(self._make_result(item, 6, "support", False, current))
                current = 0
                sub = 0
            elif prize_type == "A":
                item = self._pick(up_a_items) if up_a_items and random.random() < 0.8 else self._pick(a_items)
                results.append(self._make_result(item, 5, "support", False, sub))
                sub = 0
            elif prize_type == "exp":
                item = self._pick(support.get("exp", []))
                results.append(self._make_result(item, 4, "material", False, 0))
            elif prize_type == "overclock":
                item = self._pick(support.get("overClock", []))
                level = 3 if item is not None and "小" in item.get("name", "") else 4
                results.append(self._make_result(item, level, "material", False, 0))
            elif prize_type == "skill":
                item = self._pick(support.get("skill", []))
                results.append(self._make_result(item, 4, "material", False, 0))
            elif prize_type == "fragment":
                item = self._pick(s_items + a_items)
                result = self._make_result(item, 4, "fragment", False, 0, use_shards=True)
                result["name"] = f"支援架构零件-{result['name']}"
                results.append(result)
            else:
                item = self._pick(materials.get("cogs", []))
                results.append(self._make_result(item, 4, "material", False, 0))

        pity["current_count"] = current
        pity["sub_count"] = sub
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
        weights = pool.get("weights", {})
        current = pity["current_count"]
        sub = pity["sub_count"]
        guaranteed_up = pity["guaranteed_up"]
        types = [
            ("6", float(weights.get("6", 0))),
            ("5", float(weights.get("5", 0))),
            ("4", float(weights.get("4", 0))),
            ("3", float(weights.get("3", 0))),
            ("overclock", float(weights.get("overclock", 0))),
            ("cogs", float(weights.get("cogs", 0))),
        ]
        pity_6 = int(pool.get("total", 30))
        pity_5 = 10
        up_rate_w6 = float(pool.get("up_rate_w6", 80))
        up_w6 = pool.get("up_w6", [])
        w6_items = weapons.get("6", [])
        w6_up_items = [item for item in w6_items if item.get("name") in up_w6]

        for _ in range(count):
            current += 1
            sub += 1
            if current >= pity_6:
                item, is_up, guaranteed_up = self._pick_up(w6_up_items, w6_items, up_rate_w6, guaranteed_up)
                results.append(self._make_result(item, 6, "weapon", is_up, current))
                current = 0
                sub = 0
                continue
            if sub >= pity_5:
                item = self._pick(weapons.get("5", []))
                results.append(self._make_result(item, 5, "weapon", False, sub))
                sub = 0
                continue

            prize_type = _weighted_choice(types)
            if prize_type == "6":
                item, is_up, guaranteed_up = self._pick_up(w6_up_items, w6_items, up_rate_w6, guaranteed_up)
                results.append(self._make_result(item, 6, "weapon", is_up, current))
                current = 0
                sub = 0
            elif prize_type == "5":
                item = self._pick(weapons.get("5", []))
                results.append(self._make_result(item, 5, "weapon", False, sub))
                sub = 0
            elif prize_type == "4":
                item = self._pick(weapons.get("4", []))
                results.append(self._make_result(item, 4, "weapon", False, 0))
            elif prize_type == "3":
                item = self._pick(weapons.get("3", []))
                results.append(self._make_result(item, 3, "weapon", False, 0))
            elif prize_type == "overclock":
                item = self._pick(materials.get("overClock", []))
                results.append(self._make_result(item, 4, "material", False, 0))
            else:
                item = self._pick(materials.get("cogs", []))
                results.append(self._make_result(item, 4, "material", False, 0))

        pity["current_count"] = current
        pity["sub_count"] = sub
        pity["guaranteed_up"] = guaranteed_up
        return results


gacha_service = GachaService()
