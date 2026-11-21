"""Wide collision layer masks, symmetry and compound body inheritance."""


class CollisionLayerMatrix:
    def __init__(self, layer_count=32, max_layers=256):
        self.layer_count = layer_count
        self.max_layers = max_layers
        self.layers = {}
        self.names = {}
        self.collision = set()
        self.trigger = set()
        self.bodies = {}
        self.children = {}
        self.broadphase = []
        self.dirty = True

    def define_layer(self, name, layer_id=None):
        raise NotImplementedError

    def rename_layer(self, layer_id, new_name):
        raise NotImplementedError

    def layer_id(self, name):
        raise NotImplementedError

    def name_of(self, layer_id):
        raise NotImplementedError

    def set_collision(self, a, b, enabled=True):
        raise NotImplementedError

    def collides(self, a, b):
        raise NotImplementedError

    def set_trigger(self, a, b, enabled=True):
        raise NotImplementedError

    def trigger_collides(self, a, b):
        raise NotImplementedError

    def mask_for(self, layer):
        raise NotImplementedError

    def add_body(self, body_id, layer, enabled=True, trigger=False):
        raise NotImplementedError

    def add_child(self, parent_id, child_id, layer=None):
        raise NotImplementedError

    def set_body_layer(self, body_id, layer):
        raise NotImplementedError

    def disable_body(self, body_id):
        raise NotImplementedError

    def query_bodies(self, query_mask, trigger=False):
        raise NotImplementedError

    def pairs(self):
        raise NotImplementedError

    def rebuild(self):
        raise NotImplementedError

    def serialize(self):
        raise NotImplementedError

    def load(self, data):
        raise NotImplementedError
