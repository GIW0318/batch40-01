import threading


class AtlasFullError(Exception):
    pass


class AtlasPacker:
    def __init__(self, resolution=2048, max_resolution=8192, padding=4):
        self.max_resolution = max_resolution
        self.padding = padding
        self._warnings = []
        if not self.is_power_of_two(resolution):
            raise ValueError(
                "atlas resolution must be a power of two, got %r" % (resolution,)
            )
        if resolution > max_resolution:
            self._warnings.append(
                "requested resolution %d exceeds hardware limit %d; clamped"
                % (resolution, max_resolution)
            )
            resolution = max_resolution
        self.resolution = resolution
        self._base_resolution = resolution
        self._rects = {}
        self._islands = {}
        self._dirty = False
        self._lock = threading.Lock()

    def padding_for(self, resolution):
        return max(1, self.padding * resolution // self._base_resolution)

    def is_power_of_two(self, value):
        return isinstance(value, int) and value > 0 and (value & (value - 1)) == 0

    def pack(self, islands):
        gap = self.padding_for(self.resolution)
        candidates = [
            island for island in islands if not island.get("transparent", False)
        ]
        candidates.sort(key=lambda island: island["id"])

        rects = {}
        cursor_x = 0
        cursor_y = 0
        shelf_height = 0
        for island in candidates:
            width, height = self._effective_size(island)
            if width > self.resolution or height > self.resolution:
                raise AtlasFullError(
                    "island %r is larger than the atlas" % (island["id"],)
                )
            if cursor_x + width > self.resolution:
                cursor_x = 0
                cursor_y += shelf_height + gap
                shelf_height = 0
            if cursor_y + height > self.resolution:
                raise AtlasFullError(
                    "no space left in %dx%d atlas for island %r"
                    % (self.resolution, self.resolution, island["id"])
                )
            rects[island["id"]] = (
                float(cursor_x),
                float(cursor_y),
                float(width),
                float(height),
            )
            cursor_x += width + gap
            shelf_height = max(shelf_height, height)

        self._rects = rects
        self._islands = {island["id"]: island for island in candidates}
        return rects

    def _effective_size(self, island):
        width, height = island["size"]
        rotation = island.get("rotation", 0.0)
        if int(rotation) % 180 != 0:
            return height, width
        return width, height

    def rects(self):
        return dict(self._rects)

    def overlap_pairs(self):
        pairs = []
        ids = sorted(self._rects)
        for index, left_id in enumerate(ids):
            left = self._rects[left_id]
            for right_id in ids[index + 1:]:
                right = self._rects[right_id]
                if self._intersects(left, right):
                    pairs.append((left_id, right_id))
        return pairs

    @staticmethod
    def _intersects(left, right):
        lx, ly, lw, lh = left
        rx, ry, rw, rh = right
        return (
            lx < rx + rw
            and lx + lw > rx
            and ly < ry + rh
            and ly + lh > ry
        )

    def snap_uv(self, uv, resolution=None):
        resolution = self.resolution if resolution is None else resolution
        snapped = []
        for component in uv:
            texel = int(component * resolution)
            texel = max(0, min(texel, resolution - 1))
            snapped.append((texel + 0.5) / resolution)
        return tuple(snapped)

    def uv_for(self, island_id, local_u, local_v):
        x, y, width, height = self._rects[island_id]
        return (
            (x + local_u * width) / self.resolution,
            (y + local_v * height) / self.resolution,
        )

    def resize(self, new_resolution):
        if not self.is_power_of_two(new_resolution):
            raise ValueError(
                "atlas resolution must be a power of two, got %r" % (new_resolution,)
            )
        if new_resolution > self.max_resolution:
            self._warnings.append(
                "requested resolution %d exceeds hardware limit %d; clamped"
                % (new_resolution, self.max_resolution)
            )
            new_resolution = self.max_resolution
        if new_resolution == self.resolution:
            return
        scale = new_resolution / self.resolution
        self._rects = {
            island_id: tuple(coord * scale for coord in rect)
            for island_id, rect in self._rects.items()
        }
        self.resolution = new_resolution

    def texels_for(self, world_size, texels_per_meter):
        return int(round(world_size * texels_per_meter))

    def commit_bake(self):
        with self._lock:
            self._dirty = True

    def consume_upload(self):
        with self._lock:
            dirty = self._dirty
            self._dirty = False
            return dirty

    def validate(self):
        problems = []
        for island_id, (x, y, width, height) in self._rects.items():
            if (
                x < 0
                or y < 0
                or x + width > self.resolution
                or y + height > self.resolution
            ):
                problems.append("island %r extends outside the atlas" % (island_id,))
        problems.extend(
            "islands %r and %r overlap" % pair for pair in self.overlap_pairs()
        )
        return problems

    def warnings(self):
        return list(self._warnings)
