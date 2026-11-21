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


class LightClusterAssign:
    def __init__(self, clusters=8, near=0.1, far=100.0):
        self.clusters = clusters
        self.near = near
        self.far = far

    def slice_bounds(self, near=None, far=None, clusters=None):
        raise NotImplementedError

    def build(self, near=None, far=None, budget_ms=None, view="main"):
        raise NotImplementedError

    def view_bounds(self, view="main"):
        raise NotImplementedError

    def assign_light(self, light, view="main"):
        raise NotImplementedError

    def assign_all(self, lights, limit=None, view="main"):
        raise NotImplementedError

    def affects(self, light, point):
        raise NotImplementedError

    def shadow_slot(self, light_id):
        raise NotImplementedError

    def submit_async(self, view, frame_id, lights):
        raise NotImplementedError

    def accept_async(self, result, current_frame):
        raise NotImplementedError

    def overflow(self):
        raise NotImplementedError
