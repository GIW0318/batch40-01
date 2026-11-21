class ReflectionProbeSet:
    def __init__(self, max_face_size=1024, max_reflection_depth=2, bakes_per_tick=1):
        self.max_face_size = max_face_size
        self.max_reflection_depth = max_reflection_depth
        self.bakes_per_tick = bakes_per_tick

    def add_probe(self, probe_id, center=(0.0, 0.0), size=(2.0, 2.0),
                  rotation=0.0, baked=True, face_size=256, dynamic=False):
        raise NotImplementedError

    def remove_probe(self, probe_id):
        raise NotImplementedError

    def mark_baked(self, probe_id):
        raise NotImplementedError

    def box_projection(self, probe_id, point):
        raise NotImplementedError

    def bounds_contain(self, probe_id, point):
        raise NotImplementedError

    def weights(self, point):
        raise NotImplementedError

    def ordered_weights(self, point):
        raise NotImplementedError

    def sample_mip(self, probe_id, roughness):
        raise NotImplementedError

    def set_invalid(self, probe_id, invalid=True):
        raise NotImplementedError

    def invalid_factor(self, probe_id):
        raise NotImplementedError

    def tick(self):
        raise NotImplementedError

    def probes_for_camera(self, probe_id):
        raise NotImplementedError

    def recursion_allowed(self, depth):
        raise NotImplementedError

    def face_size(self, probe_id):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
