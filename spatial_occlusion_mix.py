class OcclusionMixer:
    def __init__(self, max_distance=50.0, ray_budget=4, head_radius=0.5):
        self.max_distance = max_distance
        self.ray_budget = ray_budget
        self.head_radius = head_radius

    def add_source(self, source_id, position, importance=1.0):
        raise NotImplementedError

    def remove_source(self, source_id):
        raise NotImplementedError

    def set_listener(self, position, forward=(0.0, 1.0)):
        raise NotImplementedError

    def add_wall(self, wall_id, start, end, door=None):
        raise NotImplementedError

    def set_door(self, door_id, closed):
        raise NotImplementedError

    def add_floor(self, floor_id, y, x_range=(-1000000.0, 1000000.0)):
        raise NotImplementedError

    def begin_frame(self, dt=0.016):
        raise NotImplementedError

    def occlusion(self, source_id):
        raise NotImplementedError

    def ray_budget_used(self):
        raise NotImplementedError

    def attenuation(self, source_id):
        raise NotImplementedError

    def audible(self):
        raise NotImplementedError

    def doppler(self, source_id, dt):
        raise NotImplementedError

    def add_reverb_zone(self, zone_id, center, radius, wet=1.0):
        raise NotImplementedError

    def reverb_at(self, position):
        raise NotImplementedError

    def pan(self, source_id):
        raise NotImplementedError

    def interaural_delay(self, source_id):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
