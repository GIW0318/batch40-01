class TextureResidencyQueue:
    def __init__(self, budget_bytes=1024, max_feedback=4, max_texture_size=4096):
        self.budget_bytes = budget_bytes
        self.max_feedback = max_feedback
        self.max_texture_size = max_texture_size

    def set_visible(self, texture_id, distance, layer=0):
        raise NotImplementedError

    def request(self, texture_id, mip, layer=0, size=(1, 1),
                compressed_bytes=0, resident_bytes=None):
        raise NotImplementedError

    def pending(self):
        raise NotImplementedError

    def next_io(self):
        raise NotImplementedError

    def finish_io(self, token):
        raise NotImplementedError

    def cancel_io(self, token):
        raise NotImplementedError

    def mark_in_use(self, texture_id, in_use=True):
        raise NotImplementedError

    def evict(self, needed_bytes):
        raise NotImplementedError

    def resident_mips(self, texture_id, layer=0):
        raise NotImplementedError

    def add_layer(self, texture_id):
        raise NotImplementedError

    def record_feedback(self, tile, weight):
        raise NotImplementedError

    def hottest_tiles(self, count=None):
        raise NotImplementedError

    def defragment(self, blocks):
        raise NotImplementedError

    def resident_bytes(self):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
