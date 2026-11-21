import heapq


class MusicStreamRing:
    def __init__(self, capacity_frames=48000, channels=2, device_rate=48000,
                 guard_frames=64):
        if capacity_frames <= 0:
            raise ValueError("capacity_frames must be positive")
        if device_rate <= 0:
            raise ValueError("device_rate must be positive")
        if guard_frames < 0:
            raise ValueError("guard_frames must not be negative")

        self.capacity_frames = int(capacity_frames)
        self.channels = int(channels)
        self.device_rate = int(device_rate)
        self.guard_frames = int(guard_frames)

        self._buffer = [None] * self.capacity_frames
        self._read_index = 0
        self._write_index = 0
        self._count = 0
        self._position = 0
        self._last_output = None

        self._decode_generation = 0
        self._next_decode_token = 1
        self._active_decodes = {}
        self._stale_tokens = set()
        self._pending_release = set()
        self._released_tracks = set()

        self._prefetch_sequence = 0
        self._prefetch_heap = []

        self._total_written = 0
        self._stale_decodes = 0
        self._underruns = 0
        self._underrun_frames = 0
        self._released = 0
        self._resampled = 0

    @property
    def _usable_capacity(self):
        return max(0, self.capacity_frames - self.guard_frames)

    def capacity(self):
        return self.capacity_frames

    def available(self):
        return self._count

    def free(self):
        return max(0, self._usable_capacity - self._count)

    def write(self, samples):
        samples = list(samples)
        count = min(len(samples), self.free())
        if count == 0:
            return 0

        capacity = self.capacity_frames
        for offset in range(count):
            index = (self._write_index + offset) % capacity
            self._buffer[index] = samples[offset]

        self._write_index = (self._write_index + count) % capacity
        self._count += count
        self._total_written += count
        return count

    def read(self, frames):
        count = min(max(0, int(frames)), self._count)
        if count == 0:
            return []

        capacity = self.capacity_frames
        output = []
        index = self._read_index
        for _ in range(count):
            output.append(self._buffer[index])
            self._buffer[index] = None
            index = (index + 1) % capacity

        self._read_index = index
        self._count -= count
        self._position += count
        if output:
            self._last_output = output[-1]
        return output

    def seek(self, frame):
        frame = max(0, int(frame))

        self._decode_generation += 1
        for decode in self._active_decodes.values():
            decode["valid"] = False

        self._buffer = [None] * self.capacity_frames
        self._read_index = 0
        self._write_index = 0
        self._count = 0
        self._position = frame
        self._last_output = None

    def begin_decode(self, track_id):
        token = self._next_decode_token
        self._next_decode_token += 1
        self._active_decodes[token] = {
            "track_id": track_id,
            "generation": self._decode_generation,
            "valid": True,
        }
        return token

    def decode_write(self, token, samples):
        decode = self._active_decodes.get(token)
        is_current = (
            decode is not None
            and decode["valid"]
            and decode["generation"] == self._decode_generation
        )
        if not is_current:
            if token not in self._stale_tokens:
                self._stale_tokens.add(token)
                self._stale_decodes += 1
            return 0
        return self.write(samples)

    def end_decode(self, token):
        decode = self._active_decodes.pop(token, None)
        if decode is None:
            return False

        track_id = decode["track_id"]
        still_decoding = any(
            active["track_id"] == track_id
            for active in self._active_decodes.values()
        )
        if track_id in self._pending_release and not still_decoding:
            self._pending_release.discard(track_id)
            self._mark_released(track_id)
        return True

    def set_device_rate(self, rate):
        rate = int(rate)
        if rate <= 0:
            raise ValueError("device_rate must be positive")
        if rate == self.device_rate:
            return self.capacity_frames

        duration_seconds = self.capacity_frames / float(self.device_rate)
        new_capacity = max(1, int(round(duration_seconds * rate)))
        self._resize(new_capacity)
        self.device_rate = rate
        return self.capacity_frames

    def capacity_for(self, rate, seconds=1.0):
        rate = int(rate)
        if rate <= 0:
            raise ValueError("rate must be positive")
        if seconds < 0:
            raise ValueError("seconds must not be negative")
        return max(1, int(round(rate * seconds)))

    def trim_priming(self, samples, priming_frames):
        priming_frames = max(0, int(priming_frames))
        return list(samples)[priming_frames:]

    def underrun(self, frames):
        count = max(0, int(frames))
        self._underruns += 1
        self._underrun_frames += count

        base = self._last_output
        if base is None:
            base = 0.0

        output = []
        value = base
        for _ in range(count):
            value *= 0.5
            output.append(value)
        if output:
            self._last_output = output[-1]
        return output

    def crossfade(self, first, first_rate, second, second_rate,
                  duration_frames):
        first = list(first)
        second = list(second)
        if not first:
            return []

        first_rate = int(first_rate)
        second_rate = int(second_rate)
        if first_rate <= 0 or second_rate <= 0:
            raise ValueError("sample rates must be positive")

        if first_rate != second_rate:
            second = self._resample(second, second_rate, first_rate)
            self._resampled += 1

        output = list(first)
        fade_frames = min(max(0, int(duration_frames)), len(first))
        available_second = min(fade_frames, len(second))
        for index in range(fade_frames):
            position = index / float(max(1, fade_frames - 1))
            first_gain = 1.0 - position
            second_gain = position
            second_sample = second[index] if index < available_second else 0.0
            output[index] = (
                first[index] * first_gain + second_sample * second_gain
            )
        return output

    def enqueue(self, track_id, priority):
        sequence = self._prefetch_sequence
        self._prefetch_sequence += 1
        heapq.heappush(
            self._prefetch_heap, (-int(priority), sequence, track_id)
        )

    def next_prefetch(self):
        if not self._prefetch_heap:
            return None
        return heapq.heappop(self._prefetch_heap)[2]

    def release_track(self, track_id):
        track_is_decoding = any(
            decode["track_id"] == track_id
            for decode in self._active_decodes.values()
        )
        if track_is_decoding:
            self._pending_release.add(track_id)
            return False

        if track_id not in self._released_tracks:
            self._mark_released(track_id)
        return True

    def allocate_pool(self, lengths):
        return {track_id: int(length) for track_id, length in lengths.items()}

    def total_written(self):
        return self._total_written

    def stats(self):
        return {
            "stale_decodes": self._stale_decodes,
            "released": self._released,
            "resampled": self._resampled,
            "underruns": self._underruns,
            "underrun_frames": self._underrun_frames,
            "counter_bits": 64,
            "capacity_frames": self.capacity_frames,
            "device_rate": self.device_rate,
            "available": self.available(),
            "free": self.free(),
        }

    def _mark_released(self, track_id):
        if track_id not in self._released_tracks:
            self._released_tracks.add(track_id)
            self._released += 1

    def _resize(self, new_capacity):
        new_capacity = max(1, int(new_capacity))
        if new_capacity == self.capacity_frames:
            return

        new_usable = max(0, new_capacity - self.guard_frames)
        existing = self._peek_all()
        if len(existing) > new_usable:
            existing = existing[-new_usable:] if new_usable else []

        self.capacity_frames = new_capacity
        self._buffer = [None] * new_capacity
        self._buffer[:len(existing)] = existing
        self._read_index = 0
        self._write_index = len(existing) % new_capacity
        self._count = len(existing)

    def _peek_all(self):
        output = []
        index = self._read_index
        for _ in range(self._count):
            output.append(self._buffer[index])
            index = (index + 1) % self.capacity_frames
        return output

    @staticmethod
    def _resample(samples, source_rate, target_rate):
        if not samples:
            return []
        if source_rate == target_rate:
            return list(samples)

        target_length = max(
            1, int(round(len(samples) * target_rate / float(source_rate)))
        )
        if len(samples) == 1:
            return [samples[0]] * target_length

        output = []
        step = source_rate / float(target_rate)
        for index in range(target_length):
            position = index * step
            lower = int(position)
            upper = min(lower + 1, len(samples) - 1)
            fraction = position - lower
            output.append(
                samples[lower] * (1.0 - fraction)
                + samples[upper] * fraction
            )
        return output
