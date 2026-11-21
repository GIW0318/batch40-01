import math
from dataclasses import dataclass


@dataclass
class _Probe:
    probe_id: object
    center: tuple
    size: tuple
    rotation: float
    baked: bool
    face_size: int
    dynamic: bool
    cos_rotation: float
    sin_rotation: float
    invalid: bool = False
    invalid_factor: float = 1.0

    @property
    def half_size(self):
        return tuple(value * 0.5 for value in self.size)


class ReflectionProbeSet:
    INVALID_FADE_STEPS = 4
    BOUNDS_EPSILON = 1e-9

    def __init__(self, max_face_size=1024, max_reflection_depth=2, bakes_per_tick=1):
        self.max_face_size = max(1, int(max_face_size))
        self.max_reflection_depth = max(0, int(max_reflection_depth))
        self.bakes_per_tick = max(0, int(bakes_per_tick))
        self._probes = {}
        self._bake_queue = []
        self._size_clamps = 0
        self._invalid_fade_step = 1.0 / self.INVALID_FADE_STEPS

    @staticmethod
    def _sort_key(probe_id):
        if isinstance(probe_id, (int, float)) and not isinstance(probe_id, bool):
            return (0, probe_id)
        return (1, str(probe_id))

    @staticmethod
    def _validate_vector(value, name):
        try:
            result = tuple(float(item) for item in value)
        except TypeError as exc:
            raise ValueError("%s must be an iterable of numbers" % name) from exc
        if len(result) < 2:
            raise ValueError("%s must have at least two components" % name)
        if not all(math.isfinite(item) for item in result):
            raise ValueError("%s must be finite" % name)
        return result

    def _probe(self, probe_id):
        return self._probes[probe_id]

    def _normalized_local(self, probe, point):
        point = tuple(float(value) for value in point)
        if len(point) != len(probe.center):
            raise ValueError("point dimensions do not match probe")

        dx = point[0] - probe.center[0]
        dy = point[1] - probe.center[1]
        local_x = dx * probe.cos_rotation + dy * probe.sin_rotation
        local_y = -dx * probe.sin_rotation + dy * probe.cos_rotation

        normalized = [local_x / probe.half_size[0], local_y / probe.half_size[1]]
        for index in range(2, len(point)):
            normalized.append(
                (point[index] - probe.center[index]) / probe.half_size[index]
            )
        return tuple(normalized)

    def add_probe(self, probe_id, center=(0.0, 0.0), size=(2.0, 2.0),
                  rotation=0.0, baked=True, face_size=256, dynamic=False):
        center = self._validate_vector(center, "center")
        size = self._validate_vector(size, "size")
        if len(center) != len(size):
            raise ValueError("center and size must have the same dimensions")
        if any(value <= 0.0 for value in size):
            raise ValueError("size components must be positive")

        rotation = float(rotation)
        if not math.isfinite(rotation):
            raise ValueError("rotation must be finite")

        requested_face_size = max(1, int(face_size))
        if requested_face_size > self.max_face_size:
            self._size_clamps += 1
        clamped_face_size = min(requested_face_size, self.max_face_size)

        self._probes.pop(probe_id, None)
        self._bake_queue = [item for item in self._bake_queue if item != probe_id]

        probe = _Probe(
            probe_id=probe_id,
            center=center,
            size=size,
            rotation=rotation,
            baked=bool(baked),
            face_size=clamped_face_size,
            dynamic=bool(dynamic),
            cos_rotation=math.cos(rotation),
            sin_rotation=math.sin(rotation),
        )
        self._probes[probe_id] = probe

        if probe.dynamic and not probe.baked:
            self._bake_queue.append(probe_id)

    def remove_probe(self, probe_id):
        self._probes.pop(probe_id, None)
        self._bake_queue = [item for item in self._bake_queue if item != probe_id]

    def mark_baked(self, probe_id):
        probe = self._probe(probe_id)
        probe.baked = True
        self._bake_queue = [item for item in self._bake_queue if item != probe_id]

    def box_projection(self, probe_id, point):
        return self._normalized_local(self._probe(probe_id), point)

    def bounds_contain(self, probe_id, point):
        normalized = self._normalized_local(self._probe(probe_id), point)
        return all(abs(value) <= 1.0 + self.BOUNDS_EPSILON for value in normalized)

    def weights(self, point):
        influences = {}
        for probe_id in sorted(self._probes, key=self._sort_key):
            probe = self._probes[probe_id]
            if not probe.baked or probe.invalid_factor <= 0.0:
                continue

            normalized = self._normalized_local(probe, point)
            distance = max(abs(value) for value in normalized)
            if distance >= 1.0:
                continue

            falloff = (1.0 - distance) ** 2
            if falloff > 0.0:
                influences[probe_id] = falloff

        total = sum(influences.values())
        if total <= 0.0:
            return {}

        return {
            probe_id: (influence / total) * self._probes[probe_id].invalid_factor
            for probe_id, influence in influences.items()
        }

    def ordered_weights(self, point):
        return sorted(
            self.weights(point).items(),
            key=lambda item: (-item[1], self._sort_key(item[0])),
        )

    def sample_mip(self, probe_id, roughness):
        probe = self._probe(probe_id)
        roughness = float(roughness)
        if not math.isfinite(roughness):
            raise ValueError("roughness must be finite")
        roughness = min(1.0, max(0.0, roughness))
        max_mip = max(0, int(math.ceil(math.log2(probe.face_size))) - 1)
        return int(math.floor(roughness * max_mip + 0.5))

    def set_invalid(self, probe_id, invalid=True):
        probe = self._probe(probe_id)
        probe.invalid = bool(invalid)
        probe.invalid_factor = 1.0

    def invalid_factor(self, probe_id):
        return self._probe(probe_id).invalid_factor

    def tick(self):
        baked = []
        while self._bake_queue and len(baked) < self.bakes_per_tick:
            probe_id = self._bake_queue.pop(0)
            probe = self._probes.get(probe_id)
            if probe is None or not probe.dynamic or probe.baked:
                continue
            probe.baked = True
            baked.append(probe_id)

        for probe in self._probes.values():
            if probe.invalid and probe.invalid_factor > 0.0:
                probe.invalid_factor = max(
                    0.0, probe.invalid_factor - self._invalid_fade_step
                )

        return baked

    def probes_for_camera(self, probe_id):
        return sorted(
            (item for item in self._probes if item != probe_id),
            key=self._sort_key,
        )

    def recursion_allowed(self, depth):
        return 0 <= depth <= self.max_reflection_depth

    def face_size(self, probe_id):
        return self._probe(probe_id).face_size

    def stats(self):
        probes = list(self._probes.values())
        return {
            "probes": len(probes),
            "baked": sum(1 for probe in probes if probe.baked),
            "unbaked": sum(1 for probe in probes if not probe.baked),
            "pending_bakes": len(self._bake_queue),
            "invalid": sum(1 for probe in probes if probe.invalid),
            "size_clamps": self._size_clamps,
        }
