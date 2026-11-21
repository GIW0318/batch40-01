"""Item binding ledger with propagation, unbinding and trade checks."""


class ItemBindingLedger:
    def __init__(self, bind_priority=None):
        self.bind_priority = dict(bind_priority or {})
        self.items = {}
        self.characters = {}
        self.events = []
        self.seen_events = set()
        self.archive = {}

    def add_character(self, character_id, account_id=None, region="r1"):
        raise NotImplementedError

    def add_item(self, item_id, owner=None, account_id=None, binding="none",
                 sockets=None):
        raise NotImplementedError

    def binding_of(self, item_id):
        raise NotImplementedError

    def item_state(self, item_id):
        raise NotImplementedError

    def character_state(self, character_id):
        raise NotImplementedError

    def archived_events(self, character_id):
        raise NotImplementedError

    def bind(self, item_id, binding, source="manual", operator=None, event_id=None):
        raise NotImplementedError

    def bind_batch(self, item_ids, binding, source="manual", operator=None,
                   event_id_prefix=None):
        raise NotImplementedError

    def unbind(self, item_id, operator=None, event_id=None):
        raise NotImplementedError

    def equip(self, character_id, item_id, event_id=None):
        raise NotImplementedError

    def unequip(self, character_id, item_id, event_id=None):
        raise NotImplementedError

    def socket(self, item_id, gem_id, event_id=None):
        raise NotImplementedError

    def unsocket(self, item_id, gem_id, event_id=None):
        raise NotImplementedError

    def enchant(self, item_id, enchant_id, source_character, event_id=None):
        raise NotImplementedError

    def trade_check(self, item_id, buyer_account=None, stage="list", event_id=None):
        raise NotImplementedError

    def mail(self, item_id, from_account, to_character, event_id=None):
        raise NotImplementedError

    def transfer_region(self, character_id, region, event_id=None):
        raise NotImplementedError

    def delete_character(self, character_id, archive=True):
        raise NotImplementedError

    def history(self, item_id=None):
        raise NotImplementedError
