"""Cinematic timeline evaluation: overlaps, loops, seeks and nested sequences."""

import math


class CinematicTrackEvaluator:
    def __init__(self, fps=60, ticks_per_second=1000):
        self.fps = fps
        self.ticks_per_second = ticks_per_second
        self.tracks = {}
        self._track_order = []
        self.clips = []
        self.time_scale = 1.0
        self.fired = set()
        self.last_seek = 0.0
        self.hold = {}
        self._current_t = 0.0

    def add_track(self, name, prop, priority=0):
        self.tracks[name] = {
            "name": name,
            "prop": prop,
            "priority": priority,
            "index": len(self._track_order),
            "offset": 0.0,
            "disabled_at": None,
        }
        self._track_order.append(name)
        return self.tracks[name]

    def add_clip(self, track, start, duration, value=None, loop=False,
                 priority=None, events=()):
        if track not in self.tracks:
            raise KeyError("unknown track: %r" % (track,))
        if duration < 0:
            raise ValueError("clip duration must be non-negative")
        clip = {
            "track": track,
            "start": float(start),
            "duration": float(duration),
            "value": value,
            "loop": loop,
            "priority": 0 if priority is None else priority,
            "events": list(events),
            "index": len(self.clips),
        }
        self.clips.append(clip)
        return clip

    def _clip_local_time(self, clip, t):
        """Map timeline time to clip-local time, or None if not active."""
        if clip["duration"] <= 0.0:
            return None
        local = t - clip["start"]
        if local < 0.0:
            return None
        if clip["loop"]:
            return local % clip["duration"]
        if local >= clip["duration"]:
            return None
        return local

    def _active_clip(self, track_name, t):
        """Highest-priority active clip on a track; ties go to the later clip."""
        best = None
        best_key = None
        for clip in self.clips:
            if clip["track"] != track_name:
                continue
            if self._clip_local_time(clip, t) is None:
                continue
            key = (clip["priority"], clip["index"])
            if best_key is None or key > best_key:
                best = clip
                best_key = key
        return best

    def evaluate(self, t):
        self._current_t = t
        values = {}
        ranks = {}
        for name in self._track_order:
            track = self.tracks[name]
            if track["disabled_at"] is not None:
                continue
            clip = self._active_clip(name, t)
            if clip is None:
                continue
            prop = track["prop"]
            rank = (track["priority"], track["index"])
            if prop not in ranks or rank >= ranks[prop]:
                ranks[prop] = rank
                values[prop] = clip["value"]
        return values

    def resolve(self, t):
        values = {}
        sources = {}
        ranks = {}
        for name in self._track_order:
            track = self.tracks[name]
            if track["disabled_at"] is not None:
                continue
            clip = self._active_clip(name, t)
            if clip is None:
                continue
            prop = track["prop"]
            rank = (track["priority"], track["index"])
            if prop not in ranks or rank >= ranks[prop]:
                ranks[prop] = rank
                values[prop] = clip["value"]
                sources[prop] = name
        return {"values": values, "sources": sources}

    def events_between(self, t0, t1):
        scale = self.time_scale if self.time_scale > 0 else 1.0
        found = []
        for clip in self.clips:
            duration = clip["duration"]
            if duration <= 0.0:
                continue
            track = self.tracks[clip["track"]]
            for event_index, event in enumerate(clip["events"]):
                at = float(event["at"])
                if at < 0.0 or at >= duration:
                    continue
                if clip["loop"]:
                    iterations = []
                    start_wall = clip["start"] + at / scale
                    period = duration / scale
                    if start_wall < t1:
                        k0 = max(0, math.ceil((t0 - start_wall) / period - 1e-9))
                        k = k0
                        while True:
                            wall = start_wall + k * period
                            if wall >= t1:
                                break
                            iterations.append(wall)
                            k += 1
                else:
                    wall = clip["start"] + at / scale
                    iterations = [wall] if t0 <= wall < t1 else []
                for wall in iterations:
                    found.append({
                        "name": event["name"],
                        "time": wall,
                        "_sort": (wall, -track["priority"], clip["index"],
                                  event_index),
                    })
        found.sort(key=lambda e: e["_sort"])
        for event in found:
            del event["_sort"]
        return found

    def seek(self, t):
        if t < self.last_seek:
            self.fired.clear()
            self.last_seek = 0.0
        events = self.events_between(self.last_seek, t)
        self.last_seek = t
        names = []
        for event in events:
            key = (event["name"], event["time"])
            if key in self.fired:
                continue
            self.fired.add(key)
            names.append(event["name"])
        return names

    def set_time_scale(self, scale):
        if scale <= 0:
            raise ValueError("time scale must be positive")
        self.time_scale = scale

    def audio_rate(self):
        return self.time_scale

    def nested_time(self, child_t, offset, child_scale=1.0):
        if child_scale <= 0:
            raise ValueError("child scale must be positive")
        return offset + child_t / child_scale

    def disable_track(self, name, at):
        track = self.tracks[name]
        if track["disabled_at"] is None:
            track["disabled_at"] = at

    def enable_track(self, name, at):
        track = self.tracks[name]
        if track["disabled_at"] is not None:
            track["offset"] += at - track["disabled_at"]
            track["disabled_at"] = None

    def track_time(self, name):
        track = self.tracks[name]
        end = track["disabled_at"]
        if end is None:
            end = self._current_t
        return end - track["offset"]

    def to_ticks(self, seconds):
        return int(round(seconds * self.ticks_per_second))

    def sample_times(self, duration, fps):
        frames = int(round(duration * fps))
        return [i / fps for i in range(frames + 1)]
