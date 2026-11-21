import math


class _Cluster:
    def __init__(self, cluster_id, parent, center, radius, vertex_count,
                 cone_axis, backface_cull):
        self.id = cluster_id
        self.parent = parent
        self.center = tuple(float(v) for v in center)
        self.radius = float(radius)
        self.vertex_count = int(vertex_count)
        axis_len = math.sqrt(sum(v * v for v in cone_axis))
        if axis_len > 0.0:
            self.cone_axis = tuple(v / axis_len for v in cone_axis)
        else:
            self.cone_axis = (0.0, 0.0, 0.0)
        self.backface_cull = bool(backface_cull)
        self.lod = 0
        self.children = []


def _merge_spheres(sphere_a, sphere_b):
    center_a, radius_a = sphere_a
    center_b, radius_b = sphere_b
    delta = tuple(b - a for a, b in zip(center_a, center_b))
    distance = math.sqrt(sum(d * d for d in delta))
    if distance + radius_a <= radius_b:
        return center_b, radius_b
    if distance + radius_b <= radius_a:
        return center_a, radius_a
    if distance == 0.0:
        radius = max(radius_a, radius_b)
        return center_a, radius
    radius = (radius_a + distance + radius_b) / 2.0
    ratio = (radius - radius_a) / distance
    center = tuple(a + ratio * d for a, d in zip(center_a, delta))
    return center, radius


class MeshletCuller:
    def __init__(self, max_vertices=255, max_depth=4):
        self.max_vertices = max_vertices
        self.max_depth = max_depth
        self._clusters = {}
        self._order = []
        self._built = False
        self._build_count = 0
        self._hierarchy_cache = {}
        self._async_jobs = {}
        self._stitches = []
        self._splits = {}
        self._visibility = {}
        self._stats = {"stale_async": 0}

    def add_cluster(self, cluster_id, parent=None, center=(0.0, 0.0, 0.0),
                    radius=1.0, vertex_count=3, cone_axis=(0.0, 1.0, 0.0),
                    backface_cull=False):
        if parent is not None and parent not in self._clusters:
            raise KeyError(parent)
        cluster = _Cluster(cluster_id, parent, center, radius, vertex_count,
                           cone_axis, backface_cull)
        self._clusters[cluster_id] = cluster
        self._order.append(cluster_id)
        if parent is not None:
            self._clusters[parent].children.append(cluster_id)
        self.mark_dirty()
        return cluster_id

    def set_lod(self, cluster_id, lod):
        self._clusters[cluster_id].lod = int(lod)
        self.mark_dirty()

    def build(self):
        if self._built:
            return
        self._hierarchy_cache = {}
        for cluster_id in reversed(self._order):
            self._compute_hierarchy(cluster_id)
        self._built = True
        self._build_count += 1

    def build_count(self):
        return self._build_count

    def mark_dirty(self):
        self._built = False
        self._hierarchy_cache = {}

    def scaled_bounds(self, cluster_id, scale=1.0):
        cluster = self._clusters[cluster_id]
        if isinstance(scale, (tuple, list)):
            scales = tuple(float(s) for s in scale)
            center = tuple(c * s for c, s in zip(cluster.center, scales))
            radius = cluster.radius * max(abs(s) for s in scales)
        else:
            factor = float(scale)
            center = tuple(c * factor for c in cluster.center)
            radius = cluster.radius * abs(factor)
        return center, radius

    def hierarchy_bounds(self, cluster_id):
        if cluster_id in self._hierarchy_cache:
            return self._hierarchy_cache[cluster_id]
        return self._compute_hierarchy(cluster_id)

    def _compute_hierarchy(self, cluster_id):
        cluster = self._clusters[cluster_id]
        sphere = (cluster.center, cluster.radius)
        for child_id in cluster.children:
            sphere = _merge_spheres(sphere, self._compute_hierarchy(child_id))
        self._hierarchy_cache[cluster_id] = sphere
        return sphere

    def vertex_count(self, cluster_id):
        return self._clusters[cluster_id].vertex_count

    def index_width(self, cluster_id):
        count = self.vertex_count(cluster_id)
        if count <= 255:
            return 8
        if count <= 65535:
            return 16
        return 32

    def cull(self, camera_pos=(0.0, 0.0, 0.0), camera_dir=(0.0, 1.0, 0.0),
             instances=None, pass_name="main"):
        self.build()
        camera_pos = tuple(float(v) for v in camera_pos)
        dir_len = math.sqrt(sum(v * v for v in camera_dir))
        camera_dir = tuple(v / dir_len for v in camera_dir)

        if instances is None:
            visible = []
            for cluster_id in self._order:
                if self._clusters[cluster_id].parent is None:
                    self._cull_recursive(cluster_id, camera_pos, camera_dir,
                                         (0.0, 0.0, 0.0), visible)
            result = visible
        else:
            result = []
            for instance in instances:
                offset = tuple(float(v) for v in instance.get("offset", (0.0, 0.0, 0.0)))
                for cluster_id in self._order:
                    if self._clusters[cluster_id].parent is None:
                        self._cull_recursive(cluster_id, camera_pos, camera_dir,
                                             offset, result,
                                             instance_id=instance.get("id"))
        self._visibility[pass_name] = result
        return result

    def _cull_recursive(self, cluster_id, camera_pos, camera_dir, offset,
                        result, instance_id=None):
        center, radius = self.hierarchy_bounds(cluster_id)
        center = tuple(c + o for c, o in zip(center, offset))
        if self._frustum_out(center, radius, camera_pos, camera_dir):
            return
        cluster = self._clusters[cluster_id]
        own_center = tuple(c + o for c, o in zip(cluster.center, offset))
        if not self._backface_out(cluster, own_center, camera_pos):
            if instance_id is None:
                result.append(cluster_id)
            else:
                result.append((cluster_id, instance_id))
        for child_id in cluster.children:
            self._cull_recursive(child_id, camera_pos, camera_dir, offset,
                                 result, instance_id)

    @staticmethod
    def _frustum_out(center, radius, camera_pos, camera_dir):
        offset = tuple(c - p for c, p in zip(center, camera_pos))
        forward = sum(o * d for o, d in zip(offset, camera_dir))
        return forward < -radius

    @staticmethod
    def _backface_out(cluster, center, camera_pos):
        if not cluster.backface_cull:
            return False
        to_camera = tuple(p - c for p, c in zip(camera_pos, center))
        if all(v == 0.0 for v in to_camera):
            return False
        dot = sum(a * b for a, b in zip(cluster.cone_axis, to_camera))
        return dot > 0.0

    def submit_async(self, frame, camera_pos=(0.0, 0.0, 0.0),
                     camera_dir=(0.0, 1.0, 0.0)):
        self._async_jobs[frame] = (
            tuple(float(v) for v in camera_pos),
            tuple(float(v) for v in camera_dir),
        )

    def apply_async(self, frame):
        job = self._async_jobs.pop(frame, None)
        if job is None:
            self._stats["stale_async"] += 1
            return []
        camera_pos, camera_dir = job
        return self.cull(camera_pos=camera_pos, camera_dir=camera_dir,
                         pass_name="async_%s" % frame)

    def stitch(self, low_id, high_id):
        low_lod = self._clusters[low_id].lod
        high_lod = self._clusters[high_id].lod
        if low_lod == high_lod:
            return []
        pair = (low_id, high_id)
        if pair not in self._stitches:
            self._stitches.append(pair)
        return [pair]

    def split_oversized(self):
        for cluster_id in list(self._order):
            if cluster_id in self._splits:
                continue
            cluster = self._clusters[cluster_id]
            if cluster.vertex_count <= self.max_vertices:
                continue
            remaining = cluster.vertex_count
            part_ids = []
            index = 0
            while remaining > 0:
                take = min(self.max_vertices, remaining)
                part_id = "%s__part%d" % (cluster_id, index)
                self.add_cluster(
                    part_id,
                    parent=cluster.parent,
                    center=cluster.center,
                    radius=cluster.radius,
                    vertex_count=take,
                    cone_axis=cluster.cone_axis,
                    backface_cull=cluster.backface_cull,
                )
                self._clusters[part_id].lod = cluster.lod
                part_ids.append(part_id)
                remaining -= take
                index += 1
            self._splits[cluster_id] = part_ids
        return dict(self._splits)

    def stats(self):
        stats = dict(self._stats)
        stats.update({
            "clusters": len(self._clusters),
            "builds": self._build_count,
            "stitches": list(self._stitches),
            "splits": dict(self._splits),
            "passes": list(self._visibility),
        })
        return stats
