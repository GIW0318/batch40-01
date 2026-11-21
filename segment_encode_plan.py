"""Segmented encoding planner: independent boundaries, ordered mux output."""

from numbers import Real


_TRACK_ORDER = {
    "video": 0,
    "audio": 1,
    "subtitle": 2,
    "data": 3,
}


class SegmentEncodePlan:
    def __init__(self, timebase=90000, audio_timebase=48000, segment_duration=2.0,
                 gop_size=30, max_bitrate=8_000_000, workers=4, priming=0.02):
        if not isinstance(timebase, int) or isinstance(timebase, bool) or timebase <= 0:
            raise ValueError("timebase must be a positive integer")
        if not isinstance(audio_timebase, int) or isinstance(audio_timebase, bool) \
                or audio_timebase <= 0:
            raise ValueError("audio_timebase must be a positive integer")
        if not isinstance(segment_duration, Real) or segment_duration <= 0:
            raise ValueError("segment_duration must be positive")
        if not isinstance(gop_size, int) or isinstance(gop_size, bool) or gop_size <= 0:
            raise ValueError("gop_size must be a positive integer")
        if not isinstance(max_bitrate, int) or isinstance(max_bitrate, bool) \
                or max_bitrate <= 0:
            raise ValueError("max_bitrate must be a positive integer")
        if not isinstance(workers, int) or isinstance(workers, bool) or workers <= 0:
            raise ValueError("workers must be a positive integer")
        if not isinstance(priming, Real) or priming < 0:
            raise ValueError("priming must be non-negative")

        self.timebase = timebase
        self.audio_timebase = audio_timebase
        self.segment_duration = float(segment_duration)
        self.gop_size = gop_size
        self.max_bitrate = max_bitrate
        self.workers = workers
        self.priming = float(priming)

    def plan(self, frames):
        if not frames:
            return []

        samples = self.timeline(frames)
        target_ticks = self.to_ticks(self.segment_duration)
        if target_ticks <= 0:
            raise ValueError("segment_duration is shorter than one timebase tick")

        ranges = []
        start = 0
        for index in range(1, len(samples)):
            elapsed = samples[index]["dts"] - samples[start]["dts"]
            if elapsed >= target_ticks:
                ranges.append((start, index))
                start = index
        ranges.append((start, len(samples)))

        bitrates = self._split_bitrate(len(ranges))
        priming_ticks = self.to_ticks(self.priming)
        segments = []

        for number, ((start, end), bitrate) in enumerate(zip(ranges, bitrates)):
            first = samples[start]
            last = samples[end - 1]
            start_pts = first["pts"]
            start_dts = first["dts"]
            end_pts = last["pts"] + last["duration"]
            end_dts = last["dts"] + last["duration"]

            segments.append({
                "segment": number,
                "frame_range": (start, end),
                "start_pts": start_pts,
                "end_pts": end_pts,
                "start_dts": start_dts,
                "end_dts": end_dts,
                "duration_ticks": end_pts - start_pts,
                "keyframes": self._keyframes(samples, start, end),
                "independent": True,
                "depends_on_previous": False,
                "closed_gop": True,
                "priming": priming_ticks,
                "bitrate": bitrate,
                "rc_debt": 0,
            })

        return segments

    def timeline(self, frames):
        samples = []
        previous_dts = None

        for frame in frames:
            try:
                dts = frame["dts"]
                pts = frame["pts"]
                duration = frame["duration"]
            except (KeyError, TypeError) as exc:
                raise ValueError("each frame needs dts, pts and duration") from exc

            if not isinstance(dts, Real) or not isinstance(pts, Real) \
                    or not isinstance(duration, Real):
                raise ValueError("frame timestamps and duration must be numeric")
            if duration <= 0:
                raise ValueError("frame duration must be positive")
            if previous_dts is not None and dts <= previous_dts:
                raise ValueError("frame DTS must be strictly increasing")

            sample = dict(frame)
            sample["dts"] = dts
            sample["pts"] = pts
            sample["duration"] = duration
            samples.append(sample)
            previous_dts = dts

        return samples

    def assign_workers(self, segments):
        return [
            {"segment": index, "worker": index % self.workers}
            for index in range(len(segments))
        ]

    def audio_plan(self, segments):
        audio = []

        for index, segment in enumerate(segments):
            start_pts = self._rescale(
                segment["start_pts"], self.timebase, self.audio_timebase
            )
            end_pts = self._rescale(
                segment["end_pts"], self.timebase, self.audio_timebase
            )
            audio.append({
                "video_segment": index,
                "start_pts": start_pts,
                "end_pts": end_pts,
                "duration_ticks": end_pts - start_pts,
                "timebase": self.audio_timebase,
            })

        return audio

    def mux_order(self, writes):
        for write in writes:
            try:
                offset = write["offset"]
                size = write["size"]
                segment = write["segment"]
                track = write["track"]
            except (KeyError, TypeError) as exc:
                raise ValueError("mux write needs segment, track, offset and size") from exc

            if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                raise ValueError("mux offsets must be non-negative integers")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ValueError("mux sizes must be non-negative integers")
            if not isinstance(segment, int) or isinstance(segment, bool) or segment < 0:
                raise ValueError("mux segment numbers must be non-negative integers")
            if not isinstance(track, str) or not track:
                raise ValueError("mux track names must be non-empty strings")

        indexed = list(enumerate(writes))
        indexed.sort(key=lambda item: (
            item[1]["segment"],
            _TRACK_ORDER.get(item[1]["track"], len(_TRACK_ORDER)),
            item[1]["offset"],
            item[0],
        ))
        return [write for _, write in indexed]

    def index(self, segments):
        entries = []

        for number, segment in enumerate(segments):
            start_pts = segment["start_pts"]
            duration_ticks = segment["duration_ticks"]
            entries.append({
                "segment": number,
                "time": self.to_seconds(start_pts),
                "pts": start_pts,
                "duration": self.to_seconds(duration_ticks),
                "duration_ticks": duration_ticks,
                "timebase": self.timebase,
            })

        return entries

    def to_ticks(self, seconds):
        return int(round(seconds * self.timebase))

    def to_seconds(self, ticks):
        return ticks / self.timebase

    @staticmethod
    def _rescale(ticks, source_timebase, target_timebase):
        return int(round(ticks * target_timebase / source_timebase))

    def _split_bitrate(self, segment_count):
        base, remainder = divmod(self.max_bitrate, segment_count)
        return [
            base + (1 if index < remainder else 0)
            for index in range(segment_count)
        ]

    def _keyframes(self, samples, start, end):
        keyframes = {start}

        for index in range(start + self.gop_size, end, self.gop_size):
            keyframes.add(index)
        for index in range(start + 1, end):
            if samples[index].get("scene_cut"):
                keyframes.add(index)

        return sorted(keyframes)
