from functools import cache
import re
import traceback
from typing import Any, Dict, List, Optional, Tuple, Union, cast

from PyPoE.poe.file.dat import DatRecord, RelationalReader
from PyPoE.poe.file.file_system import FileSystem
from PyPoE.poe.file.ot import OTFileCache
from PyPoE.poe.file.stat_filters import StatFilterFile
from PyPoE.poe.file.translations import TranslationFileCache
from PyPoE.poe.sim.formula import GemTypes, gem_stat_requirement

from RePoE.parser import Parser_Module
from RePoE.parser.constants import COOLDOWN_BYPASS_TYPES
from RePoE.parser.util import call_with_default_args, get_release_state, get_stat_translation_file_name, write_json

quality_sets = ["Superior", "Anomalous", "Divergent", "Phantasmal"]


def _handle_dict(representative, per_level):
    static = None
    cleared = True
    cleared_keys = []
    for k, v in representative.items():
        per_level_values = []
        skip = False
        for pl in per_level:
            if k not in pl:
                skip = True
                break
            per_level_values.append(pl[k])
        if skip:
            cleared = False
            continue

        if isinstance(v, dict):
            static_value, cleared_value = _handle_dict(v, per_level_values)
        elif isinstance(v, list):
            static_value, cleared_value = _handle_list(v, per_level_values)
        else:
            static_value, cleared_value = _handle_primitives(v, per_level_values)

        if static_value is not None:
            if static is None:
                static = {}
            static[k] = static_value

        if cleared_value:
            cleared_keys.append(k)
        else:
            cleared = False

    for k in cleared_keys:
        for pl in per_level:
            del pl[k]
    return static, cleared


def _handle_list(
    representative: List[Dict[str, Any]], per_level: List[List[Optional[Dict[str, Any]]]]
) -> Tuple[Optional[List[Optional[Dict[str, Any]]]], bool]:
    # edge cases (all None, any None, mismatching lengths, all empty)
    all_none = True
    any_none = False
    for pl in per_level:
        all_none &= pl is None
        any_none |= pl is None
        if pl is not None and len(pl) != len(representative):
            return None, False
    if all_none:
        return None, True
    if any_none:
        return None, False
    if not representative:
        # all empty, else above would be true
        return [], True

    static: Optional[List[Optional[Dict[str, Any]]]] = None
    cleared = True
    cleared_is = []
    for i, v in enumerate(representative):
        per_level_values = [pl[i] for pl in per_level]
        if isinstance(v, dict):
            static_value, cleared_value = _handle_dict(v, per_level_values)
        elif isinstance(v, list):
            static_value, cleared_value = _handle_list(v, per_level_values)
        else:
            static_value, cleared_value = _handle_primitives(v, per_level_values)

        if static_value is not None:
            if static is None:
                static = [None] * len(representative)
            static[i] = static_value

        if cleared_value:
            cleared_is.append(i)
        else:
            cleared = False

    for i in cleared_is:
        for pl in per_level:
            pl[i] = None
    return static, cleared


def _handle_primitives(
    representative: Union[int, str], per_level: Union[List[int], List[str]]
) -> Tuple[Union[None, int, str], bool]:
    for pl in per_level:
        if pl != representative:
            return None, False
    return representative, True


class GemConverter:
    regex_number = re.compile(r"-?\d+(\.\d+)?")

    def __init__(
        self,
        file_system: FileSystem,
        relational_reader: RelationalReader,
        translation_file_cache: TranslationFileCache,
    ) -> None:
        self.relational_reader = relational_reader
        self.translation_file_cache = translation_file_cache

        self.gepls: Dict[str, Any] = {}
        for gepl in self.relational_reader["GrantedEffectsPerLevel.dat64"]:
            ge_id = gepl["GrantedEffect"]["Id"]
            if ge_id not in self.gepls:
                self.gepls[ge_id] = []
            self.gepls[ge_id].append(gepl)

        self.gesspls: Dict[str, List[Any]] = {}
        for gesspl in self.relational_reader["GrantedEffectStatSetsPerLevel.dat64"]:
            gess_id = gesspl["StatSet"]["Id"]
            if gess_id not in self.gesspls:
                self.gesspls[gess_id] = []
            self.gesspls[gess_id].append(gesspl)

        self.granted_effect_quality_stats: Dict[str, Any] = {}
        for geq in self.relational_reader["GrantedEffectQualityStats.dat64"]:
            ge_id = geq["GrantedEffectsKey"]["Id"]
            if ge_id not in self.granted_effect_quality_stats:
                self.granted_effect_quality_stats[ge_id] = []
            self.granted_effect_quality_stats[ge_id].append(geq)

        self.tags = {}
        for tag in self.relational_reader["GemTags.dat64"]:
            name = tag["Tag"]
            self.tags[tag["Id"]] = name if name != "" else None

        self.max_levels: Dict[str, int] = {}
        for row in self.relational_reader["ItemExperiencePerLevel.dat64"]:
            base_item = row["ItemExperienceType"]["Id"]
            level = row["ItemCurrentLevel"]
            if base_item not in self.max_levels:
                self.max_levels[base_item] = level
            elif self.max_levels[base_item] < level:
                self.max_levels[base_item] = level

        self._skill_totem_life_multipliers = {}
        for row in self.relational_reader["SkillTotemVariations.dat64"]:
            self._skill_totem_life_multipliers[row["SkillTotemsKey"]] = (
                row["MonsterVarietiesKey"]["LifeMultiplier"] / 100
            )

        self.skill_stat_filter = StatFilterFile()
        self.skill_stat_filter.read(file_system.get_file("Metadata/StatDescriptions/skillpopup_stat_filters.txt"))

    def _convert_active_skill(self, active_skill: DatRecord) -> Dict[str, Any]:
        stat_conversions = {}
        for in_stat, out_stat in zip(active_skill["Input_StatKeys"], active_skill["Output_StatKeys"]):
            stat_conversions[in_stat["Id"]] = out_stat["Id"]
        skill_totem_id = active_skill["SkillTotemId"]
        is_skill_totem = skill_totem_id is not None and skill_totem_id in self._skill_totem_life_multipliers
        r = {
            "id": active_skill["Id"],
            "display_name": active_skill["DisplayedName"],
            "description": active_skill["Description"],
            "types": self._select_active_skill_types(active_skill["ActiveSkillTypes"]),
            "weapon_restrictions": [ic["Id"] for ic in active_skill["WeaponRestriction_ItemClassesKeys"]],
            "is_skill_totem": is_skill_totem,
            "is_manually_casted": active_skill["IsManuallyCasted"],
            "stat_conversions": stat_conversions,
        }
        if is_skill_totem:
            r["skill_totem_life_multiplier"] = self._skill_totem_life_multipliers[skill_totem_id]
        if active_skill["MinionActiveSkillTypes"]:
            r["minion_types"] = self._select_active_skill_types(active_skill["MinionActiveSkillTypes"])
        return r

    @classmethod
    def _convert_support_gem_specific(cls, granted_effect: DatRecord) -> Dict[str, Any]:
        return {
            "letter": granted_effect["SupportGemLetter"],
            "supports_gems_only": granted_effect["SupportsGemsOnly"],
            "allowed_types": cls._select_active_skill_types(granted_effect["AllowedActiveSkillTypes"]),
            "excluded_types": cls._select_active_skill_types(granted_effect["ExcludedActiveSkillTypes"]),
            "added_types": cls._select_active_skill_types(granted_effect["AddedActiveSkillTypes"]),
        }

    @staticmethod
    def _select_active_skill_types(type_rows: List[DatRecord]) -> List[str]:
        return [row["Id"] for row in type_rows]

    def get_translation(self, id: str, value: int):
        return self._get_translation(id, 0 if value == 0 else 1 if value > 0 else -1)

    @cache
    def _get_translation(self, id: str, value: int):
        trans = next((tr for tr in self.translations if id in tr.ids), None)
        if trans:
            try:
                lang = trans.get_language("English")
                string = lang.get_string([int(value * 20 / 1000)])[0]
                if not string or len(string.tags) > 1:
                    string = next((string for string in lang.strings if len(string.tags) == 1), lang.strings[0])
                if string:
                    s = []
                    for i, tag in enumerate(string.tags):
                        q = [str(tag)]
                        for k, v in string.quantifier.index_handlers.items():
                            if tag + 1 in v:
                                q.append(k)
                        s.append(string.strings[i])
                        s.append(f'{{{"/".join(q)}}}')
                    s.append(string.strings[-1])
                    return "".join(s)
            except:
                traceback.print_exc()
            print(id, self.game_file_name)

    def _convert_gepl(
        self,
        gepl: DatRecord,
        gess: DatRecord,
        gesspl: DatRecord,
        multipliers: Optional[Dict[str, int]],
        is_support: bool,
        xp: Optional[Dict[int, int]],
    ) -> Dict[str, Any]:
        required_level = gepl["PlayerLevelReq"]
        r = {
            "experience": xp and xp.get(gepl["Level"]),
            "required_level": int(required_level) if int(required_level) == required_level else required_level,
        }
        if gepl["Cooldown"] > 0:
            r["cooldown"] = gepl["Cooldown"]
            cooldown_bypass_type = COOLDOWN_BYPASS_TYPES(gepl["CooldownBypassType"])
            if cooldown_bypass_type is not COOLDOWN_BYPASS_TYPES.NONE:
                r["cooldown_bypass_type"] = cooldown_bypass_type.name.lower()
        if gepl["StoredUses"] > 0:
            r["stored_uses"] = gepl["StoredUses"]

        if is_support:
            r["cost_multiplier"] = gepl["CostMultiplier"]
        else:
            r["costs"] = {}
            for cost_type, cost_amount in zip(gepl["CostTypes"], gepl["CostAmounts"]):
                r["costs"][cost_type["Id"]] = cost_amount
            if gesspl["DamageEffectiveness"] != 0:
                r["damage_effectiveness"] = gesspl["DamageEffectiveness"] // 100
            if gesspl["BaseMultiplier"] != 0:
                r["damage_multiplier"] = gesspl["BaseMultiplier"]
            if gesspl["SpellCritChance"] > 0:
                r["crit_chance"] = gesspl["SpellCritChance"]
            if gepl["AttackSpeedMultiplier"] != 0:
                r["attack_speed_multiplier"] = gepl["AttackSpeedMultiplier"]
            if gepl["VaalSouls"] > 0:
                r["vaal"] = {"souls": gepl["VaalSouls"], "stored_uses": gepl["VaalStoredUses"]}

        r["reservations"] = self._convert_reservations(gepl)

        stats = []
        for k, v in zip(gesspl["FloatStats"], gesspl["BaseResolvedValues"]):
            stats.append({"id": k["Id"], "value": v, "stat": self.get_translation(k["Id"], v), "type": "float"})
        for k, v in zip(gess["ConstantStats"], gess["ConstantStatsValues"]):
            stats.append({"id": k["Id"], "value": v, "stat": self.get_translation(k["Id"], v), "type": "constant"})
        for k, v in zip(gesspl["AdditionalStats"], gesspl["AdditionalStatsValues"]):
            stats.append({"id": k["Id"], "value": v, "stat": self.get_translation(k["Id"], v), "type": "additional"})
        for k in gess["ImplicitStats"]:
            stats.append({"id": k["Id"], "value": 1, "stat": self.get_translation(k["Id"], 1), "type": "implicit"})
        for k in gesspl["AdditionalFlags"]:
            stats.append({"id": k["Id"], "value": 1, "stat": self.get_translation(k["Id"], 1), "type": "flag"})
        r["stats"] = stats

        q_stats = []
        for ge in gesspl["GrantedEffects"]:
            if ge["Id"] in self.granted_effect_quality_stats:
                for geq in self.granted_effect_quality_stats[ge["Id"]]:
                    for k, v in zip(geq["StatsKeys"], geq["StatsValuesPermille"]):
                        q_stats.append(
                            {
                                "id": k["Id"],
                                "value": v,
                                "set": geq["SetId"],
                                "set_name": quality_sets[geq["SetId"]],
                                "weight": geq["Weight"],
                                "stat": self.get_translation(k["Id"], v),
                            }
                        )
        r["quality_stats"] = q_stats

        if multipliers is not None:
            stat_requirements = {}
            gtype = GemTypes.support if is_support else GemTypes.active
            for stat_type, multi in multipliers.items():
                if multi == 0 or multi == 33 or multi == 34 or multi == 50:
                    # 33 and 34 are from white gems (Portal, Vaal Breach, Detonate Mine), which have no requirements
                    req = 0
                elif multi == 50:
                    # 50 is from SupportTutorial ("Lesser Reduced Mana Cost Support"), for which I
                    # have no idea what the requirements are.
                    print("Unknown multiplier (50) for " + gepl["GrantedEffect"]["Id"])
                    req = 0
                else:
                    req = gem_stat_requirement(gepl["PlayerLevelReq"], gtype, multi)
                stat_requirements[stat_type] = req
            r["stat_requirements"] = stat_requirements

        return r

    @staticmethod
    def _convert_reservations(gepl: DatRecord) -> Union[Dict[str, float], Dict[str, int]]:
        r = {}
        if gepl["ManaReservationFlat"] > 0:
            r["mana_flat"] = gepl["ManaReservationFlat"]
        if gepl["ManaReservationPercent"] > 0:
            r["mana_percent"] = gepl["ManaReservationPercent"] / 100
        if gepl["LifeReservationFlat"] > 0:
            r["life_flat"] = gepl["LifeReservationFlat"]
        if gepl["LifeReservationPercent"] > 0:
            r["life_percent"] = gepl["LifeReservationPercent"] / 100
        return r

    def _convert_base_item_specific(
        self, base_item_type: Optional[DatRecord], obj: Dict[str, Any], experience_type: Optional[str]
    ) -> None:
        if base_item_type is None:
            obj["base_item"] = None
            return

        obj["base_item"] = {
            "id": base_item_type["Id"],
            "display_name": base_item_type["Name"],
            "release_state": get_release_state(base_item_type["Id"]).name,
            "experience_type": experience_type,
        }
        if experience_type in self.max_levels:
            obj["base_item"]["max_level"] = self.max_levels[experience_type]

    def convert(
        self,
        base_item_type: Optional[DatRecord],
        granted_effect: DatRecord,
        secondary_granted_effect: Optional[DatRecord] = None,
        gem_tags: Optional[List[DatRecord]] = None,
        multipliers: Optional[Dict[str, int]] = None,
        xp: Optional[Dict[int, int]] = None,
        quest_reward: Optional[Dict[str, Any]] = None,
        experience_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        is_support = granted_effect["IsSupport"]
        obj = {"is_support": is_support}
        if gem_tags is None:
            obj["tags"] = None
        else:
            obj["tags"] = [tag["Id"] for tag in gem_tags]

        if is_support:
            obj["support_gem"] = self._convert_support_gem_specific(granted_effect)
        else:
            obj["cast_time"] = granted_effect["CastTime"]
            obj["active_skill"] = self._convert_active_skill(granted_effect["ActiveSkill"])

        if quest_reward:
            obj["quest_reward"] = quest_reward

        self.game_file_name = self._get_translation_file_name(obj.get("active_skill"))
        obj["stat_translation_file"] = get_stat_translation_file_name(self.game_file_name)
        self.translations = self.translation_file_cache[self.game_file_name].translations
        # lan = next((tr.languages[0] for tr in self.translations if "molten_shell_damage_absorb_limit_%_of_armour" in tr.ids), None)
        # if lan:
        #     print(self.game_file_name)
        #     for str in lan.strings:
        #         print(str.string, str.match_range([250]), str.tags_types, str.original_string)
        #     print (lan.format_string([250]))
        #     exit()

        self._convert_base_item_specific(base_item_type, obj, experience_type)

        if secondary_granted_effect:
            obj["secondary_granted_effect"] = secondary_granted_effect["Id"]

        # GrantedEffectsPerLevel
        gepls = self.gepls[granted_effect["Id"]]
        gepls.sort(key=lambda g: g["Level"])
        gess = granted_effect["StatSet"]
        gesspls = {row["GemLevel"]: row for row in self.gesspls[gess["Id"]]}
        gepls_dict = {}
        for gepl in gepls:
            gepl_converted = self._convert_gepl(gepl, gess, gesspls[gepl["Level"]], multipliers, is_support, xp)
            gepls_dict[gepl["Level"]] = gepl_converted
        obj["per_level"] = gepls_dict

        # GrantedEffectsPerLevel that do not change with level
        # makes using the json harder, but makes the json *a lot* smaller (would be like 3 times larger)
        obj["static"] = {}
        if len(gepls) >= 1:
            representative = gepls_dict[gepls[0]["Level"]]
            static, _ = _handle_dict(representative, gepls_dict.values())
            if static is not None:
                obj["static"] = static

        return obj

    @staticmethod
    def _normalize_stat_arrays(values):
        # normalize arrays for each level so they all contain the same stats (set to None if missing for a level)
        i = 0
        while i < max(len(pl["stats"]) for pl in values):
            id_map = {None: 0}
            for pl in values:
                stats = pl["stats"]
                if i >= len(stats):
                    stats.append(None)
                if stats[i] is None:
                    id_map[None] += 1
                    continue
                if stats[i]["id"] not in id_map:
                    id_map[stats[i]["id"]] = 1
                else:
                    id_map[stats[i]["id"]] += 1
            if (id_map[None] > 0 and len(id_map) > 1) or len(id_map) > 2:
                # Not all are the same stat.
                # Take the most often occurring stat except None and insert None when pl has a
                # different stat.
                del id_map[None]
                taken = max(id_map, key=lambda k: id_map[k])
                taken_text = None
                for pl in values:
                    stats = pl["stats"]
                    if stats[i] is not None:
                        if stats[i]["id"] != taken:
                            stats.insert(i, None)
                        else:
                            taken_text = stats[i]["text"]
                for pl in values:
                    stats = pl["stats"]
                    if stats[i] is None:
                        stats[i] = {"id": taken, "text": taken_text, "values": None}

            i += 1

    def _get_translation_file_name(self, active_skill: Optional[Dict[str, Any]]) -> str:
        if active_skill is None:
            return "gem_stat_descriptions.txt"
        stat_filter_group = self.skill_stat_filter.skills.get(active_skill["id"])
        if stat_filter_group is not None:
            return stat_filter_group.translation_file_path.replace("Metadata/StatDescriptions/", "")
        else:
            return "skill_stat_descriptions.txt"


class gems(Parser_Module):
    def write(self) -> None:
        gems = {}
        skill_gems = []
        relational_reader = self.relational_reader
        converter = GemConverter(self.file_system, relational_reader, self.get_cache(TranslationFileCache))
        xp: Dict[int, Dict[int, int]] = {}
        rewards: Dict[int, Dict[str, Any]] = {}

        for level in relational_reader["ItemExperiencePerLevel.dat64"]:
            rowid = level["ItemExperienceType"].rowid
            if rowid not in xp:
                xp[rowid] = {}
            xp[rowid][level["ItemCurrentLevel"]] = level["Experience"]

        for reward in relational_reader["QuestRewards.dat64"]:
            rowid = reward["Reward"].rowid
            if rowid not in rewards:
                quest = reward["RewardOffer"]["QuestKey"]
                rewards[rowid] = {"act": quest["Act"], "quest": quest["Name"], "classes": []}
            for character in reward["Characters"]:
                rewards[rowid]["classes"].append(character["Name"])

        # Skills from gems
        for gem in relational_reader["SkillGems.dat64"]:
            granted_effect = gem["GrantedEffectsKey"]
            ge_id = granted_effect["Id"]
            if ge_id in gems:
                print("Duplicate GrantedEffectsKey.Id '%s'" % ge_id)
            multipliers = {"str": gem["Str"], "dex": gem["Dex"], "int": gem["Int"]}
            gems[ge_id] = converter.convert(
                gem["BaseItemTypesKey"],
                granted_effect,
                gem["GrantedEffectsKey2"],
                gem["GemTagsKeys"],
                multipliers,
                xp.get(gem["ItemExperienceType"].rowid),
                rewards.get(gem["BaseItemTypesKey"].rowid),
                gem["ItemExperienceType"]["Id"],
            )
            skill_gems.append({k: gems[ge_id][k] for k in gems[ge_id] if k != "per_level"})

        # Secondary skills from gems. This adds the support skill implicitly provided by Bane
        for gem in relational_reader["SkillGems.dat64"]:
            granted_effect = gem["GrantedEffectsKey2"]
            if not granted_effect:
                continue
            ge_id = granted_effect["Id"]
            if ge_id in gems:
                continue
            gems[ge_id] = converter.convert(None, granted_effect)

        # Skills from mods
        for mod in relational_reader["Mods.dat64"]:
            if mod["GrantedEffectsPerLevelKeys"] is None:
                continue
            for granted_effect_per_level in mod["GrantedEffectsPerLevelKeys"]:
                granted_effect = granted_effect_per_level["GrantedEffect"]
                ge_id = granted_effect["Id"]
                if ge_id in gems:
                    # mod effects may exist as gems, those are handled above
                    continue
                gems[ge_id] = converter.convert(None, granted_effect)

        # Default Attack/PlayerMelee is neither gem nor mod effect
        for granted_effect in relational_reader["GrantedEffects.dat64"]:
            ge_id = granted_effect["Id"]
            if ge_id != "PlayerMelee":
                continue
            gems[ge_id] = converter.convert(None, granted_effect)

        write_json(gems, self.data_path, "gems")
        write_json(skill_gems, self.data_path, "gems_minimal")


if __name__ == "__main__":
    call_with_default_args(gems)
