"""MP4 mux sample tables, edit lists and ctts handling."""

from decimal import Decimal, ROUND_HALF_UP
from fractions import Fraction
from numbers import Integral


UINT32 = 1 << 32
INT32_MAX = (1 << 31) - 1


class MuxEditList:
    def __init__(self, timescale=90000, max_chunk=65536):
        self.timescale = self._positive_int(timescale, "timescale")
        self.max_chunk = self._positive_int(max_chunk, "max_chunk")
        self.tracks = {}

    @staticmethod
    def _positive_int(value, name):
        if isinstance(value, bool) or not isinstance(value, Integral):
            raise ValueError(f"{name} must be a positive integer")
        value = int(value)
        if value <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return value

    @staticmethod
    def _rescale(value, source, target):
        value = int(value)
        if source == target:
            return value
        sign = -1 if value < 0 else 1
        scaled, remainder = divmod(abs(value) * target, source)
        if remainder * 2 >= source:
            scaled += 1
        return sign * scaled

    def add_track(self, name, samples, timescale=None, priming=0):
        if not samples:
            raise ValueError("a track must contain at least one sample")
        if name in self.tracks:
            raise ValueError(f"track already exists: {name}")

        track_timescale = (
            self.timescale
            if timescale is None
            else self._positive_int(timescale, "track timescale")
        )
        priming = int(priming)
        if priming < 0:
            raise ValueError("priming must not be negative")

        prepared = []
        previous_dts = None
        wrap_offset = 0

        for source in samples:
            if not isinstance(source, dict):
                raise ValueError("samples must be dictionaries")
            try:
                dts = int(source["dts"])
                pts = int(source["pts"])
                duration = source.get("duration", 0)
                duration = 0 if duration is None else int(duration)
                size = int(source.get("size", 0))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("invalid sample fields") from exc

            if duration < 0:
                raise ValueError("sample duration must not be negative")
            if size < 0:
                raise ValueError("sample size must not be negative")

            normalized_dts = dts + wrap_offset
            if previous_dts is not None and normalized_dts < previous_dts:
                if previous_dts - normalized_dts > INT32_MAX:
                    wrap_offset += UINT32
                    normalized_dts += UINT32
                else:
                    raise ValueError("sample dts values must be monotonic")

            previous_dts = normalized_dts
            prepared_sample = dict(source)
            prepared_sample.update(
                {
                    "dts": normalized_dts,
                    "pts": pts + wrap_offset,
                    "duration": duration,
                    "size": size,
                }
            )
            prepared.append(prepared_sample)

        self.tracks[name] = {
            "name": name,
            "timescale": track_timescale,
            "priming": priming,
            "samples": prepared,
        }

    def _planned_samples(self, name):
        track = self.tracks[name]
        samples = [dict(sample) for sample in track["samples"]]

        for index, sample in enumerate(samples):
            if sample["duration"] > 0:
                continue
            if index + 1 < len(samples):
                gap = samples[index + 1]["dts"] - sample["dts"]
                if gap > 0:
                    sample["duration"] = gap
                    continue
            for previous in reversed(samples[:index]):
                if previous["duration"] > 0:
                    sample["duration"] = previous["duration"]
                    break

        source_timescale = track["timescale"]
        target_timescale = self.timescale
        for sample in samples:
            sample["dts"] = self._rescale(
                sample["dts"], source_timescale, target_timescale
            )
            sample["pts"] = self._rescale(
                sample["pts"], source_timescale, target_timescale
            )
            sample["duration"] = self._rescale(
                sample["duration"], source_timescale, target_timescale
            )

        return samples

    def plan(self):
        planned_tracks = {}
        movie_duration = 0

        for name in self.tracks:
            samples = self._planned_samples(name)
            track_end = max(
                (sample["dts"] + sample["duration"] for sample in samples),
                default=0,
            )
            movie_duration = max(movie_duration, track_end)
            planned_tracks[name] = {"samples": samples}

        return {"tracks": planned_tracks, "duration": movie_duration}

    def edit_list(self, name):
        track = self.tracks[name]
        samples = self._planned_samples(name)
        priming = self._rescale(
            track["priming"], track["timescale"], self.timescale
        )

        first_pts = samples[0]["pts"]
        trim = max(priming, -first_pts, 0)
        if trim == 0:
            return []

        total_duration = sum(sample["duration"] for sample in samples)
        segment_duration = max(0, total_duration - trim)
        return [
            {
                "media_time": trim,
                "segment_duration": segment_duration,
                "media_rate": 1,
            }
        ]

    def ctts(self, name):
        samples = self._planned_samples(name)
        offsets = [sample["pts"] - sample["dts"] for sample in samples]
        version = 1 if any(offset != 0 for offset in offsets) else 0
        return {"version": version, "offsets": offsets}

    def _track_order(self, order):
        if order is None:
            return sorted(self.tracks)

        ordered = []
        for name in order:
            if name not in self.tracks:
                raise ValueError(f"unknown track: {name}")
            if name not in ordered:
                ordered.append(name)
        ordered.extend(name for name in sorted(self.tracks) if name not in ordered)
        return ordered

    def _chunks(self, max_chunk=None, order=None):
        chunk_limit = (
            self.max_chunk
            if max_chunk is None
            else self._positive_int(max_chunk, "max_chunk")
        )
        track_order = self._track_order(order)
        ranks = {name: rank for rank, name in enumerate(track_order)}

        entries = []
        for name in track_order:
            for index, sample in enumerate(self._planned_samples(name)):
                entries.append((sample["dts"], ranks[name], index, name, sample))
        entries.sort(key=lambda entry: entry[:3])

        chunks = []
        offset = 0
        for _, _, index, name, sample in entries:
            remaining = sample["size"]
            part = 0
            while remaining > 0:
                size = min(remaining, chunk_limit)
                chunks.append(
                    {
                        "track": name,
                        "sample": index,
                        "part": part,
                        "offset": offset,
                        "size": size,
                        "dts": sample["dts"],
                        "pts": sample["pts"],
                    }
                )
                offset += size
                remaining -= size
                part += 1
        return chunks

    def interleave(self, max_chunk=None):
        return self._chunks(max_chunk=max_chunk)

    def metadata_order(self):
        return ["ftyp", "moov", "mdat"]

    def to_ticks(self, seconds):
        if isinstance(seconds, Fraction):
            return self._rescale(
                seconds.numerator * self.timescale,
                seconds.denominator,
                1,
            )
        if isinstance(seconds, Decimal):
            value = seconds
        else:
            value = Decimal(str(seconds))
        if not value.is_finite():
            raise ValueError("seconds must be finite")
        ticks = value * Decimal(self.timescale)
        return int(ticks.to_integral_value(rounding=ROUND_HALF_UP))

    def build_moov(self, order=None):
        return self._chunks(order=order)

    def duration(self):
        return self.plan()["duration"]
