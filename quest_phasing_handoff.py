"""Phase transitions with quest and NPC state handoff."""

from copy import deepcopy


class QuestPhasingHandoff:
    def __init__(self, phases=None, npcs=None, quests=None):
        self.phases = phases or {}
        self.npcs = npcs or {}
        self.quests = quests or {}
        self.characters = {}

    def add_character(self, character_id, phase=0, quests=None, markers=None):
        self.characters[character_id] = {
            "phase_id": phase,
            "quests": deepcopy(quests) if quests else {},
            "phase_markers": list(markers) if markers else [],
            "markers_stash": {},
            "instance_id": None,
        }
        return {"ok": True, "character_id": character_id}

    def _generation(self, phase_id):
        phase = self.phases.get(phase_id)
        return phase.get("generation", 0) if phase else 0

    def _stash_markers(self, char):
        phase_id = char["phase_id"]
        if phase_id and char["phase_markers"]:
            char["markers_stash"][phase_id] = {
                "generation": self._generation(phase_id),
                "markers": list(char["phase_markers"]),
            }
        char["phase_markers"] = []

    def _restore_markers(self, char, phase_id):
        entry = char["markers_stash"].pop(phase_id, None)
        if entry and entry["generation"] == self._generation(phase_id):
            return list(entry["markers"])
        return []

    def enter_phase(self, character_id, phase_id, now=0.0):
        char = self.characters.get(character_id)
        if char is None:
            return {"ok": False, "reason": "unknown_character"}
        if char["instance_id"] is not None:
            return {"ok": False, "reason": "instance_active"}
        phase = self.phases.get(phase_id)
        if phase is None:
            return {"ok": False, "reason": "unknown_phase"}
        if phase.get("locked"):
            return {"ok": False, "reason": "locked"}
        backup = (
            char["phase_id"],
            list(char["phase_markers"]),
            deepcopy(char["markers_stash"]),
            deepcopy(char["quests"]),
        )
        try:
            self._stash_markers(char)
            char["phase_id"] = phase_id
            char["phase_markers"] = self._restore_markers(char, phase_id)
        except Exception:
            (
                char["phase_id"],
                char["phase_markers"],
                char["markers_stash"],
                char["quests"],
            ) = backup
            return {"ok": False, "reason": "transition_failed"}
        return {"ok": True, "phase_id": phase_id, "generation": self._generation(phase_id)}

    def leave_phase(self, character_id, now=0.0):
        char = self.characters.get(character_id)
        if char is None:
            return {"ok": False, "reason": "unknown_character"}
        self._stash_markers(char)
        char["phase_id"] = 0
        return {"ok": True, "phase_id": 0}

    def enter_instance(self, character_id, instance_id, now=0.0):
        char = self.characters.get(character_id)
        if char is None:
            return {"ok": False, "reason": "unknown_character"}
        if char["instance_id"] is not None:
            return {"ok": False, "reason": "instance_active"}
        phase = self.phases.get(char["phase_id"], {})
        if phase.get("transitional"):
            return {"ok": False, "reason": "transitional_phase"}
        char["instance_id"] = instance_id
        return {"ok": True, "instance_id": instance_id}

    def leave_instance(self, character_id, now=0.0):
        char = self.characters.get(character_id)
        if char is None:
            return {"ok": False, "reason": "unknown_character"}
        char["instance_id"] = None
        return {"ok": True}

    def npc_state(self, character_id, npc_id):
        char = self.characters.get(character_id)
        if char is None:
            return {"npc_id": npc_id, "visible": False, "phase_id": None, "generation": 0}
        phase_id = char["phase_id"]
        phase = self.phases.get(phase_id, {})
        generation = phase.get("generation", 0)
        visible = False
        npc = self.npcs.get(npc_id)
        if (
            npc is not None
            and not phase.get("transitional")
            and npc_id in phase.get("npcs", [])
        ):
            quest_id = npc.get("quest")
            stages = npc.get("stages")
            if quest_id and stages:
                stage = char["quests"].get(quest_id, {}).get("stage")
                visible = stage in stages
            else:
                visible = True
        return {
            "npc_id": npc_id,
            "visible": visible,
            "phase_id": phase_id,
            "generation": generation,
        }

    def npc_list(self, character_id):
        char = self.characters.get(character_id)
        if char is None:
            return []
        phase = self.phases.get(char["phase_id"], {})
        if phase.get("transitional"):
            return []
        result = []
        for npc_id in phase.get("npcs", []):
            state = self.npc_state(character_id, npc_id)
            if state["visible"]:
                result.append(state)
        return result

    def phase_npcs(self, phase_id):
        phase = self.phases.get(phase_id, {})
        return list(phase.get("npcs", []))

    def merge_phases(self, source_phase, target_phase):
        source = self.phases.get(source_phase)
        target = self.phases.get(target_phase)
        if source is None or target is None:
            return {"merged": False, "reason": "unknown_phase"}
        merged = list(target.get("npcs", []))
        for npc_id in source.get("npcs", []):
            if npc_id not in merged:
                merged.append(npc_id)
        target["npcs"] = merged
        source["npcs"] = []
        for npc_id in merged:
            if npc_id in self.npcs:
                self.npcs[npc_id]["phase"] = target_phase
        for char in self.characters.values():
            if char["phase_id"] == source_phase:
                char["phase_id"] = target_phase
        return {
            "merged": True,
            "source": source_phase,
            "target": target_phase,
            "npcs": list(merged),
        }

    def bump_generation(self, phase_id):
        phase = self.phases.get(phase_id)
        if phase is None:
            return {"ok": False, "reason": "unknown_phase"}
        phase["generation"] = phase.get("generation", 0) + 1
        for char in self.characters.values():
            char["markers_stash"].pop(phase_id, None)
        return {"ok": True, "phase_id": phase_id, "generation": phase["generation"]}

    def mark(self, character_id, marker):
        char = self.characters.get(character_id)
        if char is None:
            return {"ok": False, "reason": "unknown_character"}
        if marker not in char["phase_markers"]:
            char["phase_markers"].append(marker)
        return {"ok": True}

    def set_quest_stage(self, character_id, quest_id, stage, progress=None):
        char = self.characters.get(character_id)
        if char is None:
            return {"ok": False, "reason": "unknown_character"}
        quest = char["quests"].setdefault(quest_id, {"stage": None, "progress": 0})
        quest["stage"] = stage
        if progress is not None:
            quest["progress"] = progress
        return {"ok": True, "quest_id": quest_id, "stage": stage}

    def share_quest(self, from_character, to_character, quest_id, now=0.0):
        if quest_id not in self.quests:
            return {"shared": False, "reason": "unknown_quest"}
        source = self.characters.get(from_character)
        target = self.characters.get(to_character)
        if source is None or target is None:
            return {"shared": False, "reason": "unknown_character"}
        if source["phase_id"] != target["phase_id"]:
            return {"shared": False, "reason": "phase"}
        if quest_id not in source["quests"]:
            return {"shared": False, "reason": "not_owned"}
        if quest_id in target["quests"]:
            return {"shared": False, "reason": "already_known"}
        for required in self.quests[quest_id].get("requires", []):
            if target["quests"].get(required, {}).get("stage") != "done":
                return {"shared": False, "reason": "requirements"}
        target["quests"][quest_id] = {"stage": "available", "progress": 0}
        return {"shared": True, "quest_id": quest_id}

    def sync(self, character_id):
        char = self.characters[character_id]
        return {
            "character_id": character_id,
            "phase_id": char["phase_id"],
            "generation": self._generation(char["phase_id"]),
            "quests": deepcopy(char["quests"]),
            "phase_markers": list(char["phase_markers"]),
            "markers_stash": deepcopy(char["markers_stash"]),
            "instance_id": char["instance_id"],
        }

    def restore(self, snapshot):
        stash = snapshot.get("markers_stash", {}) or {}
        self.characters[snapshot["character_id"]] = {
            "phase_id": snapshot["phase_id"],
            "quests": deepcopy(snapshot.get("quests", {})),
            "phase_markers": list(snapshot.get("phase_markers", [])),
            "markers_stash": {int(k): deepcopy(v) for k, v in stash.items()},
            "instance_id": snapshot.get("instance_id"),
        }
        return {"ok": True}
