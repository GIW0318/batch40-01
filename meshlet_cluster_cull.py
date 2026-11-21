class MeshletCuller:
    def __init__(self, max_vertices=255, max_depth=4):
        self.max_vertices = max_vertices
        self.max_depth = max_depth

    def add_cluster(self, cluster_id, parent=None, center=(0.0, 0.0, 0.0),
                    radius=1.0, vertex_count=3, cone_axis=(0.0, 1.0, 0.0),
                    backface_cull=False):
        raise NotImplementedError

    def set_lod(self, cluster_id, lod):
        raise NotImplementedError

    def build(self):
        raise NotImplementedError

    def build_count(self):
        raise NotImplementedError

    def mark_dirty(self):
        raise NotImplementedError

    def scaled_bounds(self, cluster_id, scale=1.0):
        raise NotImplementedError

    def hierarchy_bounds(self, cluster_id):
        raise NotImplementedError

    def vertex_count(self, cluster_id):
        raise NotImplementedError

    def index_width(self, cluster_id):
        raise NotImplementedError

    def cull(self, camera_pos=(0.0, 0.0, 0.0), camera_dir=(0.0, 1.0, 0.0),
             instances=None, pass_name="main"):
        raise NotImplementedError

    def submit_async(self, frame, camera_pos=(0.0, 0.0, 0.0),
                     camera_dir=(0.0, 1.0, 0.0)):
        raise NotImplementedError

    def apply_async(self, frame):
        raise NotImplementedError

    def stitch(self, low_id, high_id):
        raise NotImplementedError

    def split_oversized(self):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
