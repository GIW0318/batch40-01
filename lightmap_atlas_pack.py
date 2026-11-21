class AtlasFullError(Exception):
    pass


class AtlasPacker:
    def __init__(self, resolution=2048, max_resolution=8192, padding=4):
        self.resolution = resolution
        self.max_resolution = max_resolution
        self.padding = padding

    def padding_for(self, resolution):
        raise NotImplementedError

    def is_power_of_two(self, value):
        raise NotImplementedError

    def pack(self, islands):
        raise NotImplementedError

    def rects(self):
        raise NotImplementedError

    def overlap_pairs(self):
        raise NotImplementedError

    def snap_uv(self, uv, resolution=None):
        raise NotImplementedError

    def uv_for(self, island_id, local_u, local_v):
        raise NotImplementedError

    def resize(self, new_resolution):
        raise NotImplementedError

    def texels_for(self, world_size, texels_per_meter):
        raise NotImplementedError

    def commit_bake(self):
        raise NotImplementedError

    def consume_upload(self):
        raise NotImplementedError

    def validate(self):
        raise NotImplementedError

    def warnings(self):
        raise NotImplementedError
