"""Clustered forward light assignment (depth slices).

Contract used by the tests
--------------------------
* ``__init__(clusters=8, near=0.1, far=100.0)``.
* ``slice_bounds(near=None, far=None, clusters=None)`` returns clusters+1
  increasing boundaries from near to far using exponential interpolation:
  boundary[i] = near * (far/near) ** (i/clusters).  Consecutive gaps grow
  monotonically; near may be 0.01.
* ``build(near=None, far=None, budget_ms=None, view="main")`` stores the
  view's layout.  Estimated cost is clusters * 0.02 ms.  When budget_ms is
  given and cost exceeds it, clusters is reduced (minimum 1) until it fits.
  Returns {"view", "near", "far", "clusters", "boundaries", "cost_ms",
  "within_budget"}.  Main and reflection views keep separate layouts.
* ``view_bounds(view="main")`` returns the stored layout (KeyError before
  build).
* ``assign_light(light, view="main")``: light has id, pos (x,y,z), radius,
  type ("point"/"spot"), and for spot direction (x,y,z) and cos_outer.
  Depth is z.  A light intersects every slice whose [lo, hi) overlaps
  [z-radius, z+radius]; sorted unique slice indices are returned.  Before
  build the constructor's layout is used.
* ``assign_all(lights, limit=None, view="main")`` returns {"clusters":
  {index: [ids]}, "unassigned": [ids]}.  Each id appears at most once per
  cluster.  When limit is given and len(lights) > limit, only the first
  limit lights are assigned; the rest are unassigned and appended to
  ``overflow()``.
* ``affects(light, point)``: sphere test for point lights (distance <=
  radius); for spot lights the point must be within radius and inside the
  cone (dot(normalize(point-pos), normalize(direction)) >= cos_outer).
* ``shadow_slot(light_id)`` returns light_id & 0xFFFFFFF, so ids above
  16 bits are preserved.
* ``submit_async(view, frame_id, lights)`` returns {"view", "frame_id",
  "lights"}; ``accept_async(result, current_frame)`` is True only when
  result["frame_id"] == current_frame.
* ``overflow()`` returns ids dropped by the limit, in order.
"""

import math


class LightClusterAssign:
    def __init__(self, clusters=8, near=0.1, far=100.0):
        if int(clusters) != clusters or clusters < 1:
            raise ValueError("clusters must be a positive integer")
        self.clusters = int(clusters)
        self.near = float(near)
        self.far = float(far)
        self._validate_range(self.near, self.far)
        self._layouts = {}
        self._overflow = []

    @staticmethod
    def _validate_range(near, far):
        if not math.isfinite(near) or not math.isfinite(far):
            raise ValueError("near and far must be finite")
        if near <= 0.0 or far <= near:
            raise ValueError("expected 0 < near < far")

    def _resolve_layout(self, near=None, far=None, clusters=None):
        near = self.near if near is None else float(near)
        far = self.far if far is None else float(far)
        clusters = self.clusters if clusters is None else int(clusters)
        if clusters < 1:
            raise ValueError("clusters must be at least 1")
        self._validate_range(near, far)
        return near, far, clusters

    @staticmethod
    def _cost_ms(clusters):
        return clusters * 0.02

    def slice_bounds(self, near=None, far=None, clusters=None):
        near, far, clusters = self._resolve_layout(near, far, clusters)
        ratio = far / near
        bounds = [
            near * ratio ** (index / clusters)
            for index in range(clusters + 1)
        ]
        # Pin the endpoints so callers always see exactly the requested range.
        bounds[0] = near
        bounds[-1] = far
        return bounds

    def build(self, near=None, far=None, budget_ms=None, view="main"):
        near, far, clusters = self._resolve_layout(near, far)

        if budget_ms is not None:
            budget_ms = float(budget_ms)
            while clusters > 1 and self._cost_ms(clusters) > budget_ms:
                clusters -= 1

        cost_ms = self._cost_ms(clusters)
        layout = {
            "view": view,
            "near": near,
            "far": far,
            "clusters": clusters,
            "boundaries": self.slice_bounds(near, far, clusters),
            "cost_ms": cost_ms,
            "within_budget": budget_ms is None or cost_ms <= budget_ms,
        }
        self._layouts[view] = layout
        return layout

    def view_bounds(self, view="main"):
        return self._layouts[view]

    def _layout_for(self, view):
        if view in self._layouts:
            return self._layouts[view]
        return {
            "view": view,
            "near": self.near,
            "far": self.far,
            "clusters": self.clusters,
            "boundaries": self.slice_bounds(),
            "cost_ms": self._cost_ms(self.clusters),
            "within_budget": True,
        }

    def assign_light(self, light, view="main"):
        layout = self._layout_for(view)
        bounds = layout["boundaries"]
        z = float(light["pos"][2])
        radius = max(0.0, float(light.get("radius", 0.0)))
        light_min = z - radius
        light_max = z + radius

        result = []
        for index in range(len(bounds) - 1):
            lo = bounds[index]
            hi = bounds[index + 1]
            # Slices are [lo, hi); lights are closed intervals.  A light
            # touching an internal boundary belongs to the slice that starts
            # there, never to both neighbours.
            if light_max < lo or light_min >= hi:
                continue
            result.append(index)
        return result

    def assign_all(self, lights, limit=None, view="main"):
        lights = list(lights)
        if limit is None:
            selected = lights
            dropped = []
        else:
            limit = max(0, int(limit))
            selected = lights[:limit]
            dropped = lights[limit:]

        clusters = {
            index: []
            for index in range(self._layout_for(view)["clusters"])
        }
        seen = {index: set() for index in clusters}
        for light in selected:
            light_id = light["id"]
            for index in self.assign_light(light, view=view):
                if light_id not in seen[index]:
                    seen[index].add(light_id)
                    clusters[index].append(light_id)

        dropped_ids = [light["id"] for light in dropped]
        self._overflow = dropped_ids
        return {"clusters": clusters, "unassigned": dropped_ids}

    def affects(self, light, point):
        px, py, pz = (float(value) for value in light["pos"])
        x, y, z = (float(value) for value in point)
        dx = x - px
        dy = y - py
        dz = z - pz
        distance_sq = dx * dx + dy * dy + dz * dz
        radius = max(0.0, float(light.get("radius", 0.0)))
        if distance_sq > radius * radius:
            return False

        if light.get("type", "point") != "spot":
            return True
        if distance_sq == 0.0:
            return True

        dx_dir, dy_dir, dz_dir = (
            float(value) for value in light["direction"]
        )
        direction_length = math.sqrt(
            dx_dir * dx_dir + dy_dir * dy_dir + dz_dir * dz_dir
        )
        if direction_length == 0.0:
            return False

        cos_outer = float(light.get("cos_outer", -1.0))
        cos_angle = (
            dx * dx_dir + dy * dy_dir + dz * dz_dir
        ) / (math.sqrt(distance_sq) * direction_length)
        return cos_angle >= cos_outer

    def shadow_slot(self, light_id):
        return int(light_id) & 0xFFFFFFF

    def submit_async(self, view, frame_id, lights):
        return {
            "view": view,
            "frame_id": frame_id,
            "lights": list(lights),
        }

    def accept_async(self, result, current_frame):
        return isinstance(result, dict) and result.get("frame_id") == current_frame

    def overflow(self):
        return list(self._overflow)
