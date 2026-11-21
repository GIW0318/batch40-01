"""Batch loudness normalization with gating, channel weights and checkpoints."""

import math
import time

ABSOLUTE_GATE_LUFS = -70.0
RELATIVE_GATE_LU = 10.0
SURROUND_WEIGHT = 1.5

_LAYOUT_WEIGHTS = {
    "mono": [1.0],
    "stereo": [1.0, 1.0],
    "5.1": [1.0, 1.0, 1.0, 0.0, 1.0, 1.0],
    "7.1": [1.0, 1.0, 1.0, 0.0, 1.0, 1.0, 1.0, 1.0],
}

_SURROUND_INDEXES = {
    "mono": (),
    "stereo": (),
    "5.1": (4, 5),
    "7.1": (4, 5, 6, 7),
}


class LoudnessBatch:
    def __init__(self, target_lufs=-16.0, tolerance=0.1, true_peak_limit=-1.0,
                 window_seconds=0.4):
        self.target_lufs = target_lufs
        self.tolerance = tolerance
        self.true_peak_limit = true_peak_limit
        self.window_seconds = window_seconds

    def integrated(self, blocks):
        audible = [b for b in blocks
                   if b["lufs"] > ABSOLUTE_GATE_LUFS and b["seconds"] > 0]
        if not audible:
            return None

        def mean_energy(selected):
            total_seconds = sum(b["seconds"] for b in selected)
            return sum(10.0 ** (b["lufs"] / 10.0) * b["seconds"]
                       for b in selected) / total_seconds

        relative_gate = 10.0 * math.log10(mean_energy(audible)) - RELATIVE_GATE_LU
        gated = [b for b in audible if b["lufs"] > relative_gate]
        if not gated:
            gated = audible
        return 10.0 * math.log10(mean_energy(gated))

    def gain_for(self, measured_lufs, peak_db=None, target_lufs=None, layout="stereo"):
        if measured_lufs is None:
            return 0.0
        target = self.target_lufs if target_lufs is None else target_lufs
        gain = target - measured_lufs
        if abs(gain) <= self.tolerance:
            gain = 0.0
        gain += self.channel_correction(layout)
        if peak_db is not None:
            headroom = self.true_peak_limit - peak_db
            if gain > headroom:
                gain = headroom
        return gain

    def channel_weight(self, layout):
        return list(_LAYOUT_WEIGHTS.get(layout, _LAYOUT_WEIGHTS["stereo"]))

    def channel_correction(self, layout):
        weights = self.channel_weight(layout)
        plain_sum = sum(weights)
        if plain_sum <= 0:
            return 0.0
        surrounds = set(_SURROUND_INDEXES.get(layout, ()))
        k_weighted = sum(
            weight * (SURROUND_WEIGHT if index in surrounds else 1.0)
            for index, weight in enumerate(weights)
        )
        return 10.0 * math.log10(k_weighted / plain_sum)

    def plan(self, items, mode="single"):
        started = time.monotonic()
        entries = []
        active = []
        for source in items:
            entry = {
                "name": source["name"],
                "gain_db": 0.0,
                "skip": False,
                "reason": None,
                "stable": True,
            }
            if source.get("silent") or source["lufs"] <= ABSOLUTE_GATE_LUFS:
                entry["skip"] = True
                entry["reason"] = "silent"
            elif source.get("duration", self.window_seconds) < self.window_seconds:
                entry["skip"] = True
                entry["reason"] = "too_short"
                entry["stable"] = False
            elif abs(self.target_lufs - source["lufs"]) <= self.tolerance:
                entry["skip"] = True
                entry["reason"] = "on_target"
            else:
                active.append((source, entry))
            entries.append(entry)

        result = {"items": entries, "mode": mode}
        if mode == "album":
            album_gain = 0.0
            if active:
                blocks = [
                    {"lufs": source["lufs"],
                     "seconds": source.get("duration", 1.0)}
                    for source, _ in active
                ]
                album_lufs = self.integrated(blocks)
                album_gain = self.gain_for(album_lufs)
                for source, entry in active:
                    gain = album_gain
                    peak_db = source.get("peak_db")
                    if peak_db is not None:
                        gain = min(gain, self.true_peak_limit - peak_db)
                    entry["gain_db"] = gain
            result["album_gain_db"] = album_gain
        else:
            for source, entry in active:
                entry["gain_db"] = self.gain_for(
                    source["lufs"],
                    peak_db=source.get("peak_db"),
                    layout=source.get("layout", "stereo"),
                )

        result["elapsed_seconds"] = time.monotonic() - started
        return result

    def output_names(self, names):
        seen = {}
        resolved = []
        for name in names:
            count = seen.get(name, 0)
            if count == 0:
                resolved.append(name)
            else:
                stem, dot, ext = name.rpartition(".")
                if dot:
                    resolved.append("{}__{}.{}".format(stem, count, ext))
                else:
                    resolved.append("{}__{}".format(name, count))
            seen[name] = count + 1
        return resolved

    def merge_metadata(self, original, updated):
        merged = dict(original)
        tags = dict(original.get("tags", {}))
        tags.update(updated.get("tags", {}))
        merged["tags"] = tags
        for key, value in updated.items():
            if key != "tags":
                merged[key] = value
        if "cover" in original:
            merged["cover"] = original["cover"]
        return merged

    def checkpoint(self, done, total):
        done_count = len(done)
        return {
            "done": done_count,
            "total": total,
            "remaining": max(total - done_count, 0),
            "complete": done_count >= total,
        }

    def resume(self, items, done):
        finished = set(done)
        return [entry for entry in items if entry["name"] not in finished]
