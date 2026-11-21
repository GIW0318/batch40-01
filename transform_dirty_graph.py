class TransformGraph:
    def __init__(self):
        pass

    def add_node(self, name, parent=None, translation=(0.0, 0.0, 0.0),
                 scale=(1.0, 1.0, 1.0)):
        raise NotImplementedError

    def remove_node(self, name):
        raise NotImplementedError

    def reparent(self, name, new_parent):
        raise NotImplementedError

    def set_translation(self, name, translation):
        raise NotImplementedError

    def set_scale(self, name, scale):
        raise NotImplementedError

    def set_visible(self, name, visible):
        raise NotImplementedError

    def world_translation(self, name):
        raise NotImplementedError

    def updated_world(self, name):
        raise NotImplementedError

    def mark_dirty(self, name):
        raise NotImplementedError

    def dirty_nodes(self):
        raise NotImplementedError

    def update(self):
        raise NotImplementedError

    def set_paused(self, paused):
        raise NotImplementedError

    def visible_nodes(self):
        raise NotImplementedError

    def traversal_order(self):
        raise NotImplementedError

    def iter_snapshot(self):
        raise NotImplementedError

    def serialize(self):
        raise NotImplementedError

    @classmethod
    def deserialize(cls, text):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def instance_translation(self, prototype, override):
        raise NotImplementedError

    def normal_sign(self, name):
        raise NotImplementedError

    def rebase_origin(self, offset):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
