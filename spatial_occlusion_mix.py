import math


_SPEED_OF_SOUND = 343.0
_EPSILON = 1e-12
_RAY_COUNT = 3


def _as_point(value):
    point = tuple(float(component) for component in value)
    if len(point) < 2:
        raise ValueError("positions require at least two components")
    return point


def _subtract(left, right):
    return tuple(a - b for a, b in zip(left, right))


def _add(left, right):
    return tuple(a + b for a, b in zip(left, right))


def _scale(vector, factor):
    return tuple(component * factor for component in vector)


def _dot(left, right):
    return sum(a * b for a, b in zip(left, right))


def _length(vector):
    return math.sqrt(_dot(vector, vector))


def _normalize(vector, fallback=(0.0, 1.0)):
    magnitude = _length(vector)
    if magnitude <= _EPSILON:
        return tuple(float(component) for component in fallback)
    return _scale(vector, 1.0 / magnitude)


def _cross2d(left, right):
    return left[0] * right[1] - left[1] * right[0]


def _smoothstep(edge0, edge1, value):
    if edge0 == edge1:
        return 0.0 if value < edge0 else 1.0
    value = max(0.0, min(1.0, (value - edge0) / (edge1 - edge0)))
    return value * value * (3.0 - 2.0 * value)


def _segments_intersect(first_start, first_end, second_start, second_end):
    first_direction = _subtract(first_end, first_start)
    second_direction = _subtract(second_end, second_start)
    denominator = _cross2d(first_direction, second_direction)
    offset = _subtract(second_start, first_start)

    if abs(denominator) <= _EPSILON:
        if abs(_cross2d(offset, first_direction)) > _EPSILON:
            return False
        first_length = _dot(first_direction, first_direction)
        if first_length <= _EPSILON:
            return False
        second_start_parameter = _dot(offset, first_direction) / first_length
        second_end_parameter = _dot(
            _subtract(second_end, first_start), first_direction
        ) / first_length
        low = max(0.0, min(second_start_parameter, second_end_parameter))
        high = min(1.0, max(second_start_parameter, second_end_parameter))
        return low <= high

    first_parameter = _cross2d(offset, second_direction) / denominator
    second_parameter = _cross2d(offset, first_direction) / denominator
    return (
        -_EPSILON <= first_parameter <= 1.0 + _EPSILON
        and -_EPSILON <= second_parameter <= 1.0 + _EPSILON
    )


class OcclusionMixer:
    def __init__(self, max_distance=50.0, ray_budget=4, head_radius=0.5):
        self.max_distance = max(0.0, float(max_distance))
        self.ray_budget = max(0, int(ray_budget))
        self.head_radius = max(0.0, float(head_radius))

        self._sources = {}
        self._source_order = {}
        self._next_source_order = 0
        self._walls = {}
        self._floors = {}
        self._doors = {}
        self._reverb_zones = {}

        self._listener_position = (0.0, 0.0)
        self._listener_forward = (0.0, 1.0)

        self._frame_number = 0
        self._frame_active = False
        self._frame_dt = 0.0
        self._frame_listener_position = self._listener_position
        self._frame_listener_forward = self._listener_forward
        self._frame_listener_velocity = (0.0, 0.0)
        self._frame_sources = {}
        self._frame_walls = {}
        self._frame_floors = {}
        self._frame_doors = {}
        self._frame_reverb_zones = {}

        self._occlusion_cache = {}
        self._deferred_sources = 0
        self._frame_ray_budget_used = 0

    def add_source(self, source_id, position, importance=1.0):
        point = _as_point(position)
        if source_id not in self._sources:
            self._source_order[source_id] = self._next_source_order
            self._next_source_order += 1
        self._sources[source_id] = {
            "position": point,
            "importance": float(importance),
        }

    def remove_source(self, source_id):
        self._sources.pop(source_id, None)
        self._source_order.pop(source_id, None)
        self._occlusion_cache.pop(source_id, None)
        self._frame_sources.pop(source_id, None)

    def set_listener(self, position, forward=(0.0, 1.0)):
        self._listener_position = _as_point(position)
        self._listener_forward = _normalize(
            _as_point(forward), fallback=(0.0, 1.0)
        )

    def add_wall(self, wall_id, start, end, door=None):
        self._walls[wall_id] = {
            "start": _as_point(start),
            "end": _as_point(end),
            "door": door,
        }

    def set_door(self, door_id, closed):
        self._doors[door_id] = bool(closed)

    def add_floor(self, floor_id, y, x_range=(-1000000.0, 1000000.0)):
        low, high = (float(value) for value in x_range)
        self._floors[floor_id] = {
            "y": float(y),
            "x_range": (min(low, high), max(low, high)),
        }

    def add_reverb_zone(self, zone_id, center, radius, wet=1.0):
        self._reverb_zones[zone_id] = {
            "center": _as_point(center),
            "radius": max(0.0, float(radius)),
            "wet": max(0.0, float(wet)),
        }

    def begin_frame(self, dt=0.016):
        dt = max(0.0, float(dt))
        previous_sources = self._frame_sources
        previous_listener_position = self._frame_listener_position
        first_frame = self._frame_number == 0
        self._frame_number += 1
        self._frame_active = True
        self._frame_dt = dt

        self._frame_listener_position = self._listener_position
        self._frame_listener_forward = self._listener_forward
        if dt > _EPSILON and not first_frame:
            self._frame_listener_velocity = _scale(
                _subtract(
                    self._frame_listener_position,
                    previous_listener_position,
                ),
                1.0 / dt,
            )
        else:
            self._frame_listener_velocity = tuple(
                0.0 for _ in self._frame_listener_position
            )

        frame_sources = {}
        for source_id, source in self._sources.items():
            position = source["position"]
            previous = previous_sources.get(source_id)
            if previous is not None and dt > _EPSILON:
                velocity = _scale(
                    _subtract(position, previous["position"]), 1.0 / dt
                )
            else:
                velocity = tuple(0.0 for _ in position)
            frame_sources[source_id] = {
                "position": position,
                "importance": source["importance"],
                "velocity": velocity,
            }
        self._frame_sources = frame_sources

        self._frame_walls = dict(self._walls)
        self._frame_floors = dict(self._floors)
        self._frame_doors = dict(self._doors)
        self._frame_reverb_zones = dict(self._reverb_zones)
        self._compute_occlusion()

    def _source_for_query(self, source_id):
        if self._frame_active and source_id in self._frame_sources:
            return self._frame_sources[source_id]
        return self._sources[source_id]

    def _listener_for_query(self):
        if self._frame_active:
            return self._frame_listener_position
        return self._listener_position

    def _forward_for_query(self):
        if self._frame_active:
            return self._frame_listener_forward
        return self._listener_forward

    def _distance(self, source_id, listener=None):
        source = self._source_for_query(source_id)
        if listener is None:
            listener = self._listener_for_query()
        return _length(_subtract(source["position"], listener))

    def _is_in_range(self, source_id):
        return self._distance(source_id) <= self.max_distance + _EPSILON

    def _compute_occlusion(self):
        self._frame_ray_budget_used = 0
        self._deferred_sources = 0
        assigned = {}

        eligible = [
            source_id
            for source_id in self._sources
            if self._is_in_range(source_id)
        ]
        eligible.sort(
            key=lambda source_id: (
                -self._frame_sources[source_id]["importance"],
                self._source_order.get(source_id, 0),
            )
        )

        base_count = min(len(eligible), self.ray_budget)
        for source_id in eligible[:base_count]:
            assigned[source_id] = 1
            self._frame_ray_budget_used += 1
        self._deferred_sources = max(0, len(eligible) - base_count)

        remaining = self.ray_budget - self._frame_ray_budget_used
        while remaining > 0:
            candidate = None
            for source_id in eligible:
                if assigned.get(source_id, 0) < _RAY_COUNT:
                    candidate = source_id
                    break
            if candidate is None:
                break
            assigned[candidate] += 1
            self._frame_ray_budget_used += 1
            remaining -= 1

        for source_id, ray_count in assigned.items():
            self._occlusion_cache[source_id] = self._occlusion_for_rays(
                source_id, ray_count
            )

    def _probe_origins(self, listener, source_position, ray_count):
        direction = _normalize(
            _subtract(source_position, listener),
            fallback=self._frame_listener_forward,
        )
        right = (direction[1], -direction[0])
        offset = self.head_radius * 0.5
        origins = [listener]
        if ray_count >= 2:
            origins.append(_add(listener, _scale(right, -offset)))
        if ray_count >= 3:
            origins.append(_add(listener, _scale(right, offset)))
        return origins

    def _ray_blocked(self, start, end):
        for wall in self._frame_walls.values():
            door_id = wall["door"]
            if door_id is not None and not self._frame_doors.get(
                door_id, False
            ):
                continue
            if _segments_intersect(start, end, wall["start"], wall["end"]):
                return True

        for floor in self._frame_floors.values():
            floor_y = floor["y"]
            start_offset = start[1] - floor_y
            end_offset = end[1] - floor_y
            if start_offset * end_offset > 0.0:
                continue
            if abs(end[1] - start[1]) <= _EPSILON:
                continue
            parameter = (floor_y - start[1]) / (end[1] - start[1])
            if not -_EPSILON <= parameter <= 1.0 + _EPSILON:
                continue
            crossing_x = start[0] + parameter * (end[0] - start[0])
            low, high = floor["x_range"]
            if low - _EPSILON <= crossing_x <= high + _EPSILON:
                return True
        return False

    def _occlusion_for_rays(self, source_id, ray_count):
        if ray_count <= 0:
            return 0.0
        source_position = self._frame_sources[source_id]["position"]
        origins = self._probe_origins(
            self._frame_listener_position, source_position, ray_count
        )
        blocked = 0
        for origin in origins:
            if self._ray_blocked(origin, source_position):
                blocked += 1
        return blocked / float(ray_count)

    def occlusion(self, source_id):
        if source_id not in self._sources:
            raise KeyError(source_id)
        if not self._frame_active:
            self.begin_frame()
        return self._occlusion_cache.get(source_id, 0.0)

    def ray_budget_used(self):
        return self._frame_ray_budget_used

    def attenuation(self, source_id):
        distance = self._distance(source_id)
        if self.max_distance <= _EPSILON:
            return 1.0 if distance <= _EPSILON else 0.0
        normalized = distance / self.max_distance
        inverse_distance = 1.0 / (1.0 + normalized)
        distance_gate = 1.0 - _smoothstep(0.9, 1.0, normalized)
        if normalized >= 1.0:
            return 0.0
        return inverse_distance * distance_gate

    def audible(self):
        return [
            source_id
            for source_id in self._sources
            if self._is_in_range(source_id)
            and self.attenuation(source_id) > _EPSILON
        ]

    def doppler(self, source_id, dt):
        source = self._source_for_query(source_id)
        listener = self._listener_for_query()
        direction = _normalize(
            _subtract(source["position"], listener),
            fallback=(0.0, 0.0),
        )

        if self._frame_active:
            listener_velocity = self._frame_listener_velocity
            source_velocity = source.get("velocity", (0.0, 0.0))
        else:
            listener_velocity = (0.0, 0.0)
            source_velocity = (0.0, 0.0)

        listener_toward_source = _dot(listener_velocity, direction)
        source_toward_listener = _dot(source_velocity, direction)
        denominator = _SPEED_OF_SOUND - source_toward_listener
        if abs(denominator) <= _EPSILON:
            denominator = _EPSILON
        factor = (
            _SPEED_OF_SOUND + listener_toward_source
        ) / denominator
        return max(0.0, min(8.0, factor))

    def reverb_at(self, position):
        point = _as_point(position)
        zones = (
            self._frame_reverb_zones
            if self._frame_active
            else self._reverb_zones
        )
        reverb = 0.0
        for zone in zones.values():
            radius = zone["radius"]
            distance = _length(_subtract(point, zone["center"]))
            if radius <= _EPSILON:
                influence = 1.0 if distance <= _EPSILON else 0.0
            else:
                feather = max(radius * 0.2, _EPSILON)
                inner = max(0.0, radius - feather)
                outer = radius + feather
                if distance <= inner:
                    influence = 1.0
                elif distance >= outer:
                    influence = 0.0
                else:
                    influence = _smoothstep(outer, inner, distance)
            reverb = max(reverb, zone["wet"] * influence)
        return reverb

    def pan(self, source_id):
        source = self._source_for_query(source_id)
        listener = self._listener_for_query()
        forward = _normalize(self._forward_for_query())
        right = (forward[1], -forward[0])
        direction = _normalize(
            _subtract(source["position"], listener),
            fallback=(0.0, 0.0),
        )
        return max(-1.0, min(1.0, _dot(direction, right)))

    def interaural_delay(self, source_id):
        source = self._source_for_query(source_id)
        listener = self._listener_for_query()
        offset = _subtract(source["position"], listener)
        distance = _length(offset)
        if distance <= _EPSILON or self.head_radius <= _EPSILON:
            return 0.0
        direction = _scale(offset, 1.0 / distance)
        forward = _normalize(self._forward_for_query())
        right = (forward[1], -forward[0])
        lateral = abs(_dot(direction, right))
        return self.head_radius * lateral / _SPEED_OF_SOUND

    def stats(self):
        return {
            "frame": self._frame_number,
            "source_count": len(self._sources),
            "audible_sources": len(self.audible()),
            "ray_budget": self.ray_budget,
            "ray_budget_used": self._frame_ray_budget_used,
            "deferred_sources": self._deferred_sources,
            "occluded_sources": sum(
                1 for value in self._occlusion_cache.values() if value > 0.0
            ),
        }
