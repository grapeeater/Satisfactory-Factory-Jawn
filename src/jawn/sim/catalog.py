"""Item / recipe / building catalog for the simulator.

Values follow Satisfactory 1.x (Update 8+/1.0 era) for the parts used by the first
experiments. They are simulation inputs, not authoritative game data; extend as needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ItemDef:
    class_name: str
    name: str
    stack: int


@dataclass(frozen=True)
class RecipeDef:
    class_name: str
    name: str
    duration_s: float
    inputs: dict[str, int] = field(default_factory=dict)
    outputs: dict[str, int] = field(default_factory=dict)
    buildings: tuple[str, ...] = ()

    def per_min(self, item: str, clock: float = 1.0) -> float:
        n = self.outputs.get(item) or self.inputs.get(item) or 0
        return n * 60.0 / self.duration_s * clock


@dataclass(frozen=True)
class BuildingDef:
    class_name: str
    name: str
    power_mw: float


ITEMS: dict[str, ItemDef] = {
    i.class_name: i
    for i in [
        ItemDef("Desc_OreIron_C", "Iron Ore", 100),
        ItemDef("Desc_IronIngot_C", "Iron Ingot", 100),
        ItemDef("Desc_IronPlate_C", "Iron Plate", 200),
        ItemDef("Desc_IronRod_C", "Iron Rod", 200),
        ItemDef("Desc_IronScrew_C", "Screws", 500),
        ItemDef("Desc_IronPlateReinforced_C", "Reinforced Iron Plate", 100),
        ItemDef("Desc_OreCopper_C", "Copper Ore", 100),
        ItemDef("Desc_CopperIngot_C", "Copper Ingot", 100),
        ItemDef("Desc_Wire_C", "Wire", 500),
        ItemDef("Desc_Cable_C", "Cable", 200),
    ]
}

RECIPES: dict[str, RecipeDef] = {
    r.class_name: r
    for r in [
        RecipeDef("Recipe_IngotIron_C", "Iron Ingot", 2.0, {"Desc_OreIron_C": 1}, {"Desc_IronIngot_C": 1}, ("Build_SmelterMk1_C",)),
        RecipeDef("Recipe_IronPlate_C", "Iron Plate", 6.0, {"Desc_IronIngot_C": 3}, {"Desc_IronPlate_C": 2}, ("Build_ConstructorMk1_C",)),
        RecipeDef("Recipe_IronRod_C", "Iron Rod", 4.0, {"Desc_IronIngot_C": 1}, {"Desc_IronRod_C": 1}, ("Build_ConstructorMk1_C",)),
        RecipeDef("Recipe_Screw_C", "Screws", 6.0, {"Desc_IronRod_C": 1}, {"Desc_IronScrew_C": 4}, ("Build_ConstructorMk1_C",)),
        RecipeDef("Recipe_IronPlateReinforced_C", "Reinforced Iron Plate", 12.0, {"Desc_IronPlate_C": 6, "Desc_IronScrew_C": 12}, {"Desc_IronPlateReinforced_C": 1}, ("Build_AssemblerMk1_C",)),
        RecipeDef("Recipe_IngotCopper_C", "Copper Ingot", 2.0, {"Desc_OreCopper_C": 1}, {"Desc_CopperIngot_C": 1}, ("Build_SmelterMk1_C",)),
        RecipeDef("Recipe_Wire_C", "Wire", 4.0, {"Desc_CopperIngot_C": 1}, {"Desc_Wire_C": 2}, ("Build_ConstructorMk1_C",)),
        RecipeDef("Recipe_Cable_C", "Cable", 2.0, {"Desc_Wire_C": 2}, {"Desc_Cable_C": 1}, ("Build_ConstructorMk1_C",)),
    ]
}

BUILDINGS: dict[str, BuildingDef] = {
    b.class_name: b
    for b in [
        BuildingDef("Build_SmelterMk1_C", "Smelter", 4.0),
        BuildingDef("Build_ConstructorMk1_C", "Constructor", 4.0),
        BuildingDef("Build_AssemblerMk1_C", "Assembler", 15.0),
        BuildingDef("Build_ManufacturerMk1_C", "Manufacturer", 55.0),
    ]
}


def item(class_name: str) -> ItemDef:
    try:
        return ITEMS[class_name]
    except KeyError:
        raise KeyError(f"unknown item {class_name!r}") from None
