import math


class _Request:
    __slots__ = (
        "texture_id",
        "mip",
        "layer",
        "size",
        "compressed_bytes",
        "resident_bytes",
        "sequence",
    )

    def __init__(
        self,
        texture_id,
        mip,
        layer,
        size,
        compressed_bytes,
        resident_bytes,
        sequence,
    ):
        self.texture_id = texture_id
        self.mip = mip
        self.layer = layer
        self.size = size
        self.compressed_bytes = compressed_bytes
        self.resident_bytes = resident_bytes
        self.sequence = sequence

    @property
    def key(self):
        return (self.texture_id, self.mip, self.layer)


class _Token:
    __slots__ = ("request", "generation")

    def __init__(self, request, generation):
        self.request = request
        self.generation = generation


class TextureResidencyQueue:
    def __init__(self, budget_bytes=1024, max_feedback=4, max_texture_size=4096):
        self.budget_bytes = budget_bytes
        self.max_feedback = max_feedback
        self.max_texture_size = max_texture_size

        self._pending = []
        self._queued = {}
        self._in_flight = {}
        self._in_flight_keys = {}
        self._sequence = 0
        self._token_generation = 0

        self._resident = {}
        self._layer_counts = {}
        self._in_use = set()
        self._visibility = {}
        self._visibility_by_layer = {}
        self._last_used = {}
        self._last_used_clock = 0

        self._feedback = {}
        self._feedback_sequence = 0

        self._duplicates = 0
        self._evictions = 0
        self._cancelled = 0
        self._oversized = 0
        self._sync_waits = 0

    def set_visible(self, texture_id, distance, layer=0):
        if distance is None:
            distance = math.inf
        distance = float(distance)

        self._visibility_by_layer[(texture_id, layer)] = distance
        previous = self._visibility.get(texture_id)
        self._visibility[texture_id] = (
            distance if previous is None else min(previous, distance)
        )
        self._touch(texture_id)

    def request(
        self,
        texture_id,
        mip,
        layer=0,
        size=(1, 1),
        compressed_bytes=0,
        resident_bytes=None,
    ):
        mip = int(mip)
        layer = int(layer)
        if mip < 0:
            raise ValueError("mip level must be non-negative")
        if layer < 0:
            raise ValueError("layer must be non-negative")

        size = tuple(size)
        if len(size) != 2:
            raise ValueError("size must contain width and height")
        width, height = int(size[0]), int(size[1])
        if width < 1 or height < 1:
            raise ValueError("texture dimensions must be positive")

        if self.max_texture_size is not None:
            limit = int(self.max_texture_size)
            if width > limit or height > limit:
                self._oversized += 1
                raise ValueError(
                    "texture size {}x{} exceeds platform limit {}".format(
                        width, height, limit
                    )
                )

        compressed_bytes = int(compressed_bytes)
        if compressed_bytes < 0:
            raise ValueError("compressed_bytes must be non-negative")
        if resident_bytes is None:
            resident_bytes = width * height * 4
        resident_bytes = int(resident_bytes)
        if resident_bytes < 0:
            raise ValueError("resident_bytes must be non-negative")

        self._ensure_layer(texture_id, layer)
        key = (texture_id, mip, layer)
        if key in self._queued or key in self._in_flight_keys:
            self._duplicates += 1
            return False
        if mip in self._resident[(texture_id, layer)]:
            self._duplicates += 1
            return False

        request = _Request(
            texture_id,
            mip,
            layer,
            (width, height),
            compressed_bytes,
            resident_bytes,
            self._sequence,
        )
        self._sequence += 1
        self._pending.append(request)
        self._queued[key] = request
        self._touch(texture_id)
        return True

    def pending(self):
        requests = sorted(self._pending, key=self._priority)
        return [(item.texture_id, item.mip, item.layer) for item in requests]

    def next_io(self):
        if not self._pending:
            return None

        request = min(self._pending, key=self._priority)
        self._pending.remove(request)
        self._queued.pop(request.key, None)

        self._token_generation += 1
        token = _Token(request, self._token_generation)
        self._in_flight[token] = request
        self._in_flight_keys[request.key] = token
        return token

    def finish_io(self, token):
        request = self._in_flight.pop(token, None)
        if request is None:
            return False

        if self._in_flight_keys.get(request.key) is token:
            del self._in_flight_keys[request.key]

        resident = self._resident.setdefault(
            (request.texture_id, request.layer), {}
        )
        if request.mip in resident:
            return False
        if any(mip <= request.mip for mip in resident):
            return False

        if (
            self.budget_bytes is not None
            and self.resident_bytes() + request.resident_bytes > self.budget_bytes
        ):
            raise MemoryError(
                "resident texture budget exceeded: {} + {} > {}".format(
                    self.resident_bytes(),
                    request.resident_bytes,
                    self.budget_bytes,
                )
            )

        resident[request.mip] = request.resident_bytes
        self._touch(request.texture_id)
        return True

    def cancel_io(self, token):
        request = self._in_flight.pop(token, None)
        if request is None:
            return False

        if self._in_flight_keys.get(request.key) is token:
            del self._in_flight_keys[request.key]
        self._cancelled += 1
        return True

    def mark_in_use(self, texture_id, in_use=True):
        if in_use:
            self._in_use.add(texture_id)
        else:
            self._in_use.discard(texture_id)
        self._touch(texture_id)

    def evict(self, needed_bytes):
        needed_bytes = int(needed_bytes)
        if needed_bytes <= 0:
            return []

        candidates = [
            (texture_id, layer)
            for (texture_id, layer), mips in self._resident.items()
            if mips and texture_id not in self._in_use
        ]
        evictable = sum(
            size
            for texture_id, layer in candidates
            for size in self._resident[(texture_id, layer)].values()
        )
        if evictable < needed_bytes:
            raise MemoryError(
                "cannot evict {} resident bytes; only {} are evictable".format(
                    needed_bytes, evictable
                )
            )

        candidates.sort(key=self._eviction_priority)
        freed = 0
        evicted = []
        for texture_id, layer in candidates:
            if freed >= needed_bytes:
                break
            mips = self._resident.get((texture_id, layer), {})
            freed += sum(mips.values())
            if mips:
                self._resident[(texture_id, layer)] = {}
            evicted.append(texture_id)
            self._evictions += 1

        result = []
        seen = set()
        for texture_id in evicted:
            if texture_id not in seen:
                seen.add(texture_id)
                result.append(texture_id)
        return result

    def resident_mips(self, texture_id, layer=0):
        return sorted(self._resident.get((texture_id, layer), {}))

    def add_layer(self, texture_id):
        count = self._layer_counts.get(texture_id, 1)
        self._layer_counts[texture_id] = count + 1
        self._resident.setdefault((texture_id, count), {})
        self._touch(texture_id)
        return count

    def record_feedback(self, tile, weight):
        weight = float(weight)
        self._feedback_sequence += 1
        self._feedback[tile] = (weight, self._feedback_sequence)

        if self.max_feedback is not None and len(self._feedback) > self.max_feedback:
            excess = len(self._feedback) - self.max_feedback
            coldest = sorted(
                self._feedback,
                key=lambda item: (
                    self._feedback[item][0],
                    self._feedback[item][1],
                ),
            )
            for item in coldest[:excess]:
                del self._feedback[item]

    def hottest_tiles(self, count=None):
        ranked = sorted(
            self._feedback,
            key=lambda item: (
                -self._feedback[item][0],
                self._feedback[item][1],
            ),
        )
        if count is not None:
            ranked = ranked[: int(count)]
        return ranked

    def defragment(self, blocks):
        movable = []
        for block in blocks:
            texture_id = self._block_texture_id(block)
            if texture_id not in self._in_use:
                movable.append(block)
        return movable

    def resident_bytes(self):
        return sum(
            size
            for mips in self._resident.values()
            for size in mips.values()
        )

    def stats(self):
        return {
            "budget_bytes": self.budget_bytes,
            "resident_bytes": self.resident_bytes(),
            "pending": len(self._pending),
            "in_flight": len(self._in_flight),
            "duplicates": self._duplicates,
            "evictions": self._evictions,
            "cancelled": self._cancelled,
            "oversized": self._oversized,
            "sync_waits": self._sync_waits,
        }

    def _priority(self, request):
        layer_distance = self._visibility_by_layer.get(
            (request.texture_id, request.layer)
        )
        texture_distance = self._visibility.get(request.texture_id)
        if layer_distance is not None:
            return (0, layer_distance, request.sequence)
        if texture_distance is not None:
            return (0, texture_distance, request.sequence)
        return (1, math.inf, request.sequence)

    def _touch(self, texture_id):
        self._last_used_clock += 1
        self._last_used[texture_id] = self._last_used_clock

    def _eviction_priority(self, resident_key):
        texture_id, layer = resident_key
        distance = self._visibility_by_layer.get(
            (texture_id, layer), self._visibility.get(texture_id, math.inf)
        )
        return (-distance, self._last_used.get(texture_id, -1), texture_id, layer)

    def _ensure_layer(self, texture_id, layer):
        count = self._layer_counts.get(texture_id, 1)
        self._layer_counts[texture_id] = max(count, layer + 1)
        for index in range(layer + 1):
            self._resident.setdefault((texture_id, index), {})

    @staticmethod
    def _block_texture_id(block):
        if isinstance(block, (tuple, list)):
            return block[0]
        return block
