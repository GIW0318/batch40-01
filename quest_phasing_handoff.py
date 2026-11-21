"""Phase transitions with quest and NPC state handoff."""


class QuestPhasingHandoff:
    def __init__(self, phases=None, npcs=None, quests=None):
        self.phases = phases or {}
        self.npcs = npcs or {}
        self.quests = quests or {}
        self.characters = {}

    def add_character(self, character_id, phase=0, quests=None, markers=None):
        raise NotImplementedError

    def enter_phase(self, character_id, phase_id, now=0.0):
        raise NotImplementedError

    def leave_phase(self, character_id, now=0.0):
        raise NotImplementedError

    def enter_instance(self, character_id, instance_id, now=0.0):
        raise NotImplementedError

    def leave_instance(self, character_id, now=0.0):
        raise NotImplementedError

    def npc_state(self, character_id, npc_id):
        raise NotImplementedError

    def npc_list(self, character_id):
        raise NotImplementedError

    def phase_npcs(self, phase_id):
        raise NotImplementedError

    def merge_phases(self, source_phase, target_phase):
        raise NotImplementedError

    def bump_generation(self, phase_id):
        raise NotImplementedError

    def mark(self, character_id, marker):
        raise NotImplementedError

    def set_quest_stage(self, character_id, quest_id, stage, progress=None):
        raise NotImplementedError

    def share_quest(self, from_character, to_character, quest_id, now=0.0):
        raise NotImplementedError

    def sync(self, character_id):
        raise NotImplementedError

    def restore(self, snapshot):
        raise NotImplementedError
