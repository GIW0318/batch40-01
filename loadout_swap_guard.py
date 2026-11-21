"""loadout_swap_guard (initial skeleton)."""


class LoadoutSwapGuard:
    """Equipment loadout guard.

    register_character stores item dicts keyed by item_id with slot, attack, set,
    optional two_handed, owner and deleted, plus equipped and bag lists,
    bag_capacity, hotbar and skills.  save validates ownership, deletion,
    duplicates and the two-handed/offhand conflict, and enforces that an item
    belongs to only one loadout; a loadout stores item_ids plus hotbar and skills.
    preview returns valid, added, removed, bag_slots_needed, free_slots and errors;
    bag_slots_needed is the number of removed items that must enter the bag minus
    the added items already in the bag.  apply refuses when in_combat, before
    cooldown_until or when the swap is invalid; a successful swap is transactional
    (context fail_after rolls everything back), moves exactly the diff, replaces
    hotbar and skills and recomputes stats; apply returns applied, changed, added,
    removed and stats.  stats: attack is the sum of equipped
    attack, set_bonus is 10 per set with at least two equipped pieces, total is
    their sum.  Applying the same loadout twice does not stack stats.  revert
    restores the previous equipment, bag, hotbar, skills and stats and returns
    reverted, equipped and stats.  state returns
    copies of equipped, bag, bag_capacity, hotbar, skills, stats and locked.
    preview and apply errors include 'not enough bag slots', 'deleted:<id>',
    'not owned:<id>', 'item already in loadout:<id>' and 'two-handed with
    offhand'; apply refusal reasons include in_combat, cooldown and
    transaction_failed.
    """

    def __init__(self):
        raise NotImplementedError()

    def register_character(self, character_id, items=None, equipped=None, bag=None, bag_capacity=6, hotbar=None, skills=None):
        raise NotImplementedError()

    def add_item(self, character_id, item):
        raise NotImplementedError()

    def delete_item(self, character_id, item_id):
        raise NotImplementedError()

    def stats(self, character_id):
        raise NotImplementedError()

    def state(self, character_id):
        raise NotImplementedError()

    def save(self, character_id, name, item_ids, hotbar=None, skills=None):
        raise NotImplementedError()

    def preview(self, character_id, name):
        raise NotImplementedError()

    def apply(self, character_id, name, context=None):
        raise NotImplementedError()

    def revert(self, character_id):
        raise NotImplementedError()
