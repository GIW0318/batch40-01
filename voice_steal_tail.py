"""Voice stealing with release envelopes, virtual voices and reverb tails."""

import math
import threading


class VoiceStealPool:
    _CLICK_FREE_FADE = 0.001

    def __init__(self, max_voices=32, release_time=0.05, tail_time=1.0, reserved=None):
        self.max_voices = max(0, int(max_voices))
        self.release_time = max(0.0, float(release_time))
        self.tail_time = max(0.0, float(tail_time))
        self.reserved = {
            bus: max(0, int(count)) for bus, count in dict(reserved or {}).items()
        }
        self._next_id = 1
        self._next_generation = 1
        self._lock = threading.RLock()
        self.active = {}
        self.virtual = {}
        self.releasing = []
        self.tails = []

    def acquire(self, sound_id, priority=0, distance=0.0, duration=1.0,
                bus="sfx", loop=False, channels=2, fade_in=0.0, fade_out=0.0):
        values = self._voice_values(
            priority=priority,
            distance=distance,
            duration=duration,
            channels=channels,
            fade_in=fade_in,
            fade_out=fade_out,
        )
        with self._lock:
            if len(self.active) >= self.max_voices:
                victim_key = self._select_victim(values["priority"])
                if victim_key is None:
                    return None
                self._steal(victim_key)

            voice_id = self._next_id
            self._next_id += 1
            generation = self._new_generation()
            key = (voice_id, generation)
            entry = self._make_entry(
                key,
                sound_id=sound_id,
                bus=bus,
                loop=bool(loop),
                **values
            )
            self.active[key] = entry
            return self._public_handle(entry)

    def release(self, handle, fade=0.02):
        with self._lock:
            key = self._handle_key(handle)
            entry = self.active.pop(key, None)
            if entry is None:
                return False
            release_fade = max(
                float(fade), entry["fade_out"], self._CLICK_FREE_FADE
            )
            self._begin_release(entry, release_fade)
            self._add_tail(entry)
            return True

    def virtualize(self, handle):
        with self._lock:
            key = self._handle_key(handle)
            entry = self.active.pop(key, None)
            if entry is None:
                return False
            self.virtual[key] = entry
            return True

    def restore(self, handle):
        with self._lock:
            key = self._handle_key(handle)
            entry = self.virtual.get(key)
            if entry is None:
                return None

            if len(self.active) >= self.max_voices:
                victim_key = self._select_victim(entry["priority"])
                if victim_key is None:
                    return None
                self._steal(victim_key)

            del self.virtual[key]
            generation = self._new_generation()
            restored_key = (entry["id"], generation)
            restored = dict(entry)
            restored["key"] = restored_key
            restored["generation"] = generation
            self.active[restored_key] = restored
            return self._public_handle(restored)

    def advance(self, dt):
        dt = max(0.0, float(dt))
        with self._lock:
            for entry in list(self.active.values()):
                entry["elapsed"] += dt

            for entry in list(self.virtual.values()):
                entry["elapsed"] += dt

            for entry in self.releasing:
                entry["remaining"] -= dt
            self.releasing = [
                entry for entry in self.releasing if entry["remaining"] > 0.0
            ]

            for tail in self.tails:
                tail["remaining"] -= dt
            self.tails = [
                tail for tail in self.tails if tail["remaining"] > 0.0
            ]

    def is_active(self, handle):
        with self._lock:
            return self._handle_key(handle) in self.active

    def position(self, handle):
        with self._lock:
            key = self._handle_key(handle)
            entry = self.active.get(key)
            if entry is None:
                entry = self.virtual.get(key)
            if entry is None:
                return None

            elapsed = float(entry["elapsed"])
            duration = entry["duration"]
            if entry["loop"] and duration > 0.0:
                playhead = elapsed % duration
            else:
                playhead = min(elapsed, duration)
            return {
                "elapsed": elapsed,
                "playhead": playhead,
                "remaining": self._remaining(entry),
                "loop": entry["loop"],
            }

    def snapshot(self):
        with self._lock:
            buses = {}
            for entry in self.active.values():
                usage = buses.setdefault(
                    entry["bus"], {"voices": 0, "channels": 0}
                )
                usage["voices"] += 1
                usage["channels"] += entry["channels"]

            for bus in self.reserved:
                buses.setdefault(bus, {"voices": 0, "channels": 0})

            return {
                "voices": len(self.active),
                "channels": sum(e["channels"] for e in self.active.values()),
                "virtual": len(self.virtual),
                "releasing": len(self.releasing),
                "tails": len(self.tails),
                "buses": buses,
            }

    def _voice_values(self, priority, distance, duration, channels, fade_in, fade_out):
        return {
            "priority": float(priority),
            "distance": float(distance),
            "duration": max(0.0, float(duration)),
            "channels": max(1, int(channels)),
            "fade_in": max(0.0, float(fade_in)),
            "fade_out": max(0.0, float(fade_out)),
        }

    def _new_generation(self):
        generation = self._next_generation
        self._next_generation += 1
        return generation

    def _make_entry(self, key, sound_id, bus, loop, **values):
        entry = {
            "key": key,
            "id": key[0],
            "generation": key[1],
            "sound_id": sound_id,
            "bus": bus,
            "loop": loop,
            "elapsed": 0.0,
        }
        entry.update(values)
        return entry

    def _public_handle(self, entry):
        return {
            "id": entry["id"],
            "generation": entry["generation"],
            "sound_id": entry["sound_id"],
            "priority": entry["priority"],
            "distance": entry["distance"],
            "duration": entry["duration"],
            "bus": entry["bus"],
            "loop": entry["loop"],
            "channels": entry["channels"],
            "fade_in": entry["fade_in"],
            "fade_out": entry["fade_out"],
            "elapsed": entry["elapsed"],
        }

    def _handle_key(self, handle):
        if not isinstance(handle, dict):
            return None
        voice_id = handle.get("id")
        generation = handle.get("generation")
        if not isinstance(voice_id, int) or not isinstance(generation, int):
            return None
        return (voice_id, generation)

    def _remaining(self, entry):
        if entry["loop"]:
            return math.inf
        return max(0.0, entry["duration"] - entry["elapsed"])

    def _select_victim(self, incoming_priority):
        bus_voices = {}
        for entry in self.active.values():
            bus = entry["bus"]
            bus_voices[bus] = bus_voices.get(bus, 0) + 1

        candidates = []
        for key, entry in self.active.items():
            reserved = self.reserved.get(entry["bus"], 0)
            if reserved and bus_voices[entry["bus"]] <= reserved:
                continue
            if entry["priority"] > incoming_priority:
                continue
            candidates.append(
                (
                    entry["priority"],
                    -entry["distance"],
                    self._remaining(entry),
                    entry["id"],
                    entry["generation"],
                    key,
                )
            )

        if not candidates:
            return None
        return min(candidates)[-1]

    def _steal(self, key):
        entry = self.active.pop(key)
        release_fade = max(
            self.release_time, entry["fade_out"], self._CLICK_FREE_FADE
        )
        self._begin_release(entry, release_fade)
        self._add_tail(entry)

    def _begin_release(self, entry, fade):
        fade = max(float(fade), self._CLICK_FREE_FADE)
        self.releasing.append({
            "id": entry["id"],
            "generation": entry["generation"],
            "sound_id": entry["sound_id"],
            "bus": entry["bus"],
            "channels": entry["channels"],
            "fade": fade,
            "remaining": fade,
            "elapsed": entry["elapsed"],
            "click_free": True,
        })

    def _add_tail(self, entry):
        if self.tail_time <= 0.0:
            return
        self.tails.append({
            "id": entry["id"],
            "generation": entry["generation"],
            "sound_id": entry["sound_id"],
            "bus": entry["bus"],
            "channels": entry["channels"],
            "remaining": self.tail_time,
            "duration": self.tail_time,
        })
