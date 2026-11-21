from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, field

_LOG = logging.getLogger(__name__)

_LAYOUT_CHANNELS = {
    "mono": 1,
    "1.0": 1,
    "stereo": 2,
    "2.0": 2,
    "quad": 4,
    "4.0": 4,
    "5.0": 5,
    "5.1": 6,
    "6.0": 6,
    "7.0": 7,
    "7.1": 8,
    "5.1.2": 8,
    "5.1.4": 10,
    "7.1.2": 10,
    "7.1.4": 12,
}

_LAYOUT_NAMES = {
    1: ("M",),
    2: ("L", "R"),
    4: ("L", "R", "Ls", "Rs"),
    5: ("L", "R", "C", "Ls", "Rs"),
    6: ("L", "R", "C", "LFE", "Ls", "Rs"),
    7: ("L", "R", "C", "LFE", "Ls", "Rs", "Cs"),
    8: ("L", "R", "C", "LFE", "Ls", "Rs", "Lb", "Rb"),
    10: ("L", "R", "C", "LFE", "Ls", "Rs", "Lb", "Rb", "Tl", "Tr"),
    12: ("L", "R", "C", "LFE", "Ls", "Rs", "Lb", "Rb", "Tfl", "Tfr", "Tbl", "Tbr"),
}


@dataclass
class _GainRamp:
    start: float
    target: float
    duration_ms: float
    elapsed_ms: float = 0.0

    def advance(self, ms: float) -> tuple[float, bool]:
        self.elapsed_ms = min(self.duration_ms, self.elapsed_ms + ms)
        if self.duration_ms <= 0.0:
            return self.target, True
        progress = self.elapsed_ms / self.duration_ms
        value = self.start + (self.target - self.start) * progress
        return value, self.elapsed_ms >= self.duration_ms

    def copy(self) -> _GainRamp:
        return _GainRamp(
            start=self.start,
            target=self.target,
            duration_ms=self.duration_ms,
            elapsed_ms=self.elapsed_ms,
        )


@dataclass
class _Bus:
    name: str
    parent: str | None
    gain: float = 1.0
    sends: dict[str, dict] = field(default_factory=dict)
    ramp: _GainRamp | None = None
    buffers: list[list[float]] = field(default_factory=list)
    generation: int = 0

    @property
    def buffer(self) -> list[list[float]]:
        return self.buffers

    def allocate(
        self,
        channels: int,
        frames: int,
        previous: list[list[float]] | None = None,
    ) -> None:
        new_buffers = [[0.0 for _ in range(frames)] for _ in range(channels)]
        if previous:
            for channel in range(min(channels, len(previous))):
                count = min(frames, len(previous[channel]))
                new_buffers[channel][:count] = previous[channel][:count]
        self.buffers = new_buffers


@dataclass(frozen=True)
class _Voice:
    voice_id: str
    bus: str


@dataclass
class _PendingRebuild:
    routes: tuple[tuple[str, str | None], ...]
    parents: dict[str, str | None]
    order: list[str]
    candidate: dict[str, _Bus]
    index: int = 0

    @property
    def remaining(self) -> int:
        return len(self.order) - self.index


class AudioBusGraph:
    def __init__(self, max_depth=4, sample_rate=48000, channels=2):
        if not isinstance(max_depth, int) or max_depth < 0:
            raise ValueError("max_depth must be a non-negative integer")
        if not isinstance(sample_rate, int) or sample_rate <= 0:
            raise ValueError("sample_rate must be a positive integer")
        if not isinstance(channels, int) or channels <= 0:
            raise ValueError("channels must be a positive integer")

        self.max_depth = max_depth
        self.sample_rate = sample_rate
        self.channels = channels
        self._buffer_seconds = 256.0 / 48000.0
        self._channel_layout = self._layout_name(channels)
        self._lock = threading.RLock()
        self._version = 0
        self._buses: dict[str, _Bus] = {}
        self._voices: dict[str, _Voice] = {}
        self._pending: _PendingRebuild | None = None
        self._retired: list[dict[str, _Bus]] = []
        self._rebuild_steps = 0
        self._commits = 0
        self._last_reason = "none"

        master = _Bus("master", None, generation=0)
        master.allocate(self.channels, self._frames_per_buffer())
        self._buses["master"] = master

    def add_bus(self, name, parent=None, gain=1.0):
        name = self._validate_name(name)
        gain = self._validate_gain(gain)
        with self._lock:
            if name in self._buses:
                raise ValueError(f"bus already exists: {name}")
            if parent is not None:
                parent = self._validate_name(parent)
                if parent not in self._buses:
                    raise ValueError(f"unknown parent bus: {parent}")
                depth = self._local_depth(parent) + 1
                if depth > self.max_depth:
                    raise ValueError(
                        f"bus depth {depth} exceeds max_depth {self.max_depth}: {name}"
                    )
            bus = _Bus(name, parent, gain=gain, generation=self._version)
            bus.allocate(self.channels, self._frames_per_buffer())
            self._buses[name] = bus
            self._version += 1

    def add_voice(self, voice_id, bus):
        voice_id = self._validate_name(voice_id)
        bus = self._validate_name(bus)
        with self._lock:
            if bus not in self._buses:
                raise ValueError(f"unknown bus: {bus}")
            if voice_id in self._voices:
                raise ValueError(f"voice already active: {voice_id}")
            self._voices[voice_id] = _Voice(voice_id, bus)

    def remove_voice(self, voice_id):
        voice_id = self._validate_name(voice_id)
        with self._lock:
            return self._voices.pop(voice_id, None) is not None

    def connect(self, bus, parent):
        bus = self._validate_name(bus)
        parent = self._validate_name(parent)
        with self._lock:
            if bus not in self._buses:
                raise ValueError(f"unknown bus: {bus}")
            if parent not in self._buses:
                raise ValueError(f"unknown parent bus: {parent}")
            if bus == "master":
                raise ValueError("master cannot be reparented")

            cursor = parent
            while cursor is not None:
                if cursor == bus:
                    raise ValueError(f"routing cycle would be created: {bus} -> {parent}")
                cursor = self._buses[cursor].parent

            new_depth = self._local_depth(parent)
            subtree_depth = self._subtree_height(bus)
            if new_depth + 1 + subtree_depth > self.max_depth:
                raise ValueError(
                    f"bus depth exceeds max_depth {self.max_depth}: {bus}"
                )
            if self._buses[bus].parent == parent:
                return
            self._buses[bus].parent = parent
            self._version += 1

    def validate(self):
        with self._lock:
            issues: list[tuple] = []
            if "master" not in self._buses:
                issues.append(("missing", "master"))

            for name, bus in self._buses.items():
                if name == "master":
                    if bus.parent is not None:
                        issues.append(("root_parent", name, bus.parent))
                    continue

                if bus.parent is None:
                    issues.append(("orphan", name))
                    continue
                if bus.parent not in self._buses:
                    issues.append(("missing_parent", name, bus.parent))
                    continue

                depth, reaches_master, missing_parent, cycle = self._route_info(name)
                if cycle:
                    issues.append(("cycle", name))
                    continue
                if missing_parent is not None:
                    issues.append(("missing_parent", name, missing_parent))
                elif not reaches_master:
                    issues.append(("orphan", name))
                elif depth > self.max_depth:
                    issues.append(("depth", name, depth))

            for issue in issues:
                _LOG.warning("audio bus graph validation issue: %s", issue)
            return issues

    def rebuild(self, routes=None, max_steps=None):
        if max_steps is not None and (
            not isinstance(max_steps, int) or max_steps < 0
        ):
            raise ValueError("max_steps must be a non-negative integer")

        with self._lock:
            if routes is not None:
                try:
                    normalized = self._normalize_routes(routes)
                except ValueError as exc:
                    _LOG.error("audio bus graph rebuild rejected: %s", exc)
                    raise
                if self._pending is None or self._pending.routes != normalized:
                    self._pending = _PendingRebuild(
                        routes=normalized,
                        parents=dict(normalized),
                        order=self._topological_order(normalized),
                        candidate={},
                    )
            elif self._pending is None:
                raise ValueError("no rebuild is pending")

            pending = self._pending
            steps = pending.remaining if max_steps is None else min(max_steps, pending.remaining)
            for _ in range(steps):
                self._build_pending_step(pending)

            if pending.remaining:
                self._last_reason = "budget"
                return {
                    "applied": False,
                    "reason": "budget",
                    "pending": pending.remaining,
                    "built": pending.index,
                }

            blockers = sorted(
                voice.voice_id
                for voice in self._voices.values()
                if voice.bus not in pending.candidate
            )
            if blockers:
                self._last_reason = "voices"
                return {
                    "applied": False,
                    "reason": "voices",
                    "pending": 0,
                    "voices": blockers,
                }

            old_names = set(self._buses)
            retired = self._buses
            new_buses = pending.candidate
            new_generation = self._version + 1
            self._inherit_live_state(new_buses)
            for bus in new_buses.values():
                bus.generation = new_generation

            self._buses = new_buses
            self._retired.append(retired)
            if len(self._retired) > 2:
                self._retired.pop(0)
            self._pending = None
            self._version = new_generation
            self._commits += 1
            self._last_reason = "applied"
            return {
                "applied": True,
                "reason": "applied",
                "pending": 0,
                "destroyed": sorted(old_names - set(new_buses)),
                "version": self._version,
            }

    def graph_version(self):
        with self._lock:
            return self._version

    def active_bus(self, name):
        name = self._validate_name(name)
        with self._lock:
            return name in self._buses

    def set_gain(self, bus, gain, ramp_ms=100):
        bus = self._validate_name(bus)
        gain = self._validate_gain(gain)
        if not isinstance(ramp_ms, (int, float)) or ramp_ms < 0:
            raise ValueError("ramp_ms must be non-negative")
        with self._lock:
            node = self._require_bus(bus)
            if ramp_ms == 0:
                node.gain = gain
                node.ramp = None
                return
            node.ramp = _GainRamp(
                start=node.gain,
                target=gain,
                duration_ms=float(ramp_ms),
            )

    def gain_at(self, bus):
        bus = self._validate_name(bus)
        with self._lock:
            return self._require_bus(bus).gain

    def tick(self, ms):
        if not isinstance(ms, (int, float)) or ms < 0:
            raise ValueError("ms must be non-negative")
        with self._lock:
            for bus in self._buses.values():
                if bus.ramp is None:
                    continue
                value, finished = bus.ramp.advance(float(ms))
                bus.gain = value
                if finished:
                    bus.gain = bus.ramp.target
                    bus.ramp = None

    def set_channel_layout(self, layout):
        count = self._channels_for_layout(layout)
        with self._lock:
            if count == self.channels:
                return count
            old_buses = self._buses
            frames = self._frames_per_buffer()
            new_buses: dict[str, _Bus] = {}
            for name, old in old_buses.items():
                node = self._clone_bus(old, old.parent, self._version + 1)
                node.allocate(count, frames, previous=old.buffers)
                new_buses[name] = node
            self._buses = new_buses
            self._retired.append(old_buses)
            if len(self._retired) > 2:
                self._retired.pop(0)
            self.channels = count
            self._channel_layout = self._layout_name(count)
            self._version += 1
            return count

    def channel_count(self):
        with self._lock:
            return self.channels

    def set_sample_rate(self, rate):
        if not isinstance(rate, int) or rate <= 0:
            raise ValueError("sample_rate must be a positive integer")
        with self._lock:
            if rate == self.sample_rate:
                return
            old_buses = self._buses
            frames = max(1, round(rate * self._buffer_seconds))
            new_buses: dict[str, _Bus] = {}
            for name, old in old_buses.items():
                node = self._clone_bus(old, old.parent, self._version + 1)
                node.allocate(self.channels, frames, previous=old.buffers)
                new_buses[name] = node
            self._buses = new_buses
            self._retired.append(old_buses)
            if len(self._retired) > 2:
                self._retired.pop(0)
            self.sample_rate = rate
            self._version += 1

    def latency_samples(self):
        with self._lock:
            return round(self.sample_rate * 0.010)

    def set_send(self, bus, target, amount=0.0, pre_fader=True):
        bus = self._validate_name(bus)
        target = self._validate_name(target)
        amount = self._validate_amount(amount)
        with self._lock:
            node = self._require_bus(bus)
            self._require_bus(target)
            if bus == target:
                raise ValueError("a bus cannot send to itself")
            node.sends[target] = {
                "amount": amount,
                "pre_fader": bool(pre_fader),
            }

    def send_config(self, bus, target):
        bus = self._validate_name(bus)
        target = self._validate_name(target)
        with self._lock:
            node = self._require_bus(bus)
            config = node.sends.get(target)
            return dict(config) if config is not None else None

    def snapshot(self, name, sends):
        name = self._validate_name(name)
        normalized = self._normalize_sends(sends)
        with self._lock:
            self._require_bus(name)
            return {
                "name": name,
                "bus": name,
                "sends": normalized,
                "graph_version": self._version,
                "sample_rate": self.sample_rate,
                "channels": self.channels,
            }

    def apply_snapshot(self, snapshot):
        if not isinstance(snapshot, dict):
            raise TypeError("snapshot must be a mapping")
        name = snapshot.get("bus", snapshot.get("name"))
        name = self._validate_name(name)
        sends = snapshot.get("sends", {})
        with self._lock:
            node = self._require_bus(name)
            normalized = self._normalize_sends(sends)
            for target in normalized:
                self._require_bus(target)
                if target == name:
                    raise ValueError("a bus cannot send to itself")
            node.sends = normalized
            return True

    def stats(self):
        with self._lock:
            return {
                "main_thread_ms": 0.0,
                "audio_thread_ms": 0.0,
                "rebuild_steps": self._rebuild_steps,
                "commits": self._commits,
                "pending_steps": self._pending.remaining if self._pending else 0,
                "pending_rebuild": self._pending is not None,
                "last_rebuild_reason": self._last_reason,
                "voices": len(self._voices),
                "buses": len(self._buses),
                "retired_generations": len(self._retired),
                "graph_version": self._version,
                "sample_rate": self.sample_rate,
                "channels": self.channels,
                "latency_samples": round(self.sample_rate * 0.010),
                "buffer_channels": {
                    name: len(bus.buffers) for name, bus in self._buses.items()
                },
            }

    def _build_pending_step(self, pending: _PendingRebuild) -> None:
        name = pending.order[pending.index]
        old = self._buses.get(name)
        if old is None:
            node = _Bus(name, pending.parents[name], generation=self._version + 1)
            node.allocate(self.channels, self._frames_per_buffer())
        else:
            node = self._clone_bus(old, pending.parents[name], self._version + 1)
            valid_targets = set(pending.parents)
            node.sends = {
                target: dict(config)
                for target, config in node.sends.items()
                if target in valid_targets
            }
        pending.candidate[name] = node
        pending.index += 1
        self._rebuild_steps += 1

    def _clone_bus(self, bus: _Bus, parent: str | None, generation: int) -> _Bus:
        node = _Bus(
            name=bus.name,
            parent=parent,
            gain=bus.gain,
            sends={target: dict(config) for target, config in bus.sends.items()},
            ramp=bus.ramp.copy() if bus.ramp is not None else None,
            generation=generation,
        )
        node.allocate(self.channels, self._frames_per_buffer(), previous=bus.buffers)
        return node

    def _inherit_live_state(self, new_buses: dict[str, _Bus]) -> None:
        frames = self._frames_per_buffer()
        valid_targets = set(new_buses)
        for name, new in new_buses.items():
            old = self._buses.get(name)
            if old is not None:
                new.gain = old.gain
                new.ramp = old.ramp.copy() if old.ramp is not None else None
                new.sends = {
                    target: dict(config)
                    for target, config in old.sends.items()
                    if target in valid_targets
                }
            if len(new.buffers) != self.channels or any(
                len(channel) != frames for channel in new.buffers
            ):
                previous = old.buffers if old is not None else new.buffers
                new.allocate(self.channels, frames, previous=previous)

    def _normalize_routes(
        self, routes
    ) -> tuple[tuple[str, str | None], ...]:
        if isinstance(routes, dict):
            items = list(routes.items())
        else:
            try:
                items = list(routes)
            except TypeError as exc:
                raise ValueError("routes must be an iterable of (bus, parent)") from exc

        pairs: list[tuple[str, str | None]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ValueError("each route must be a (bus, parent) pair")
            name, parent = item
            name = self._validate_name(name)
            if name in seen:
                raise ValueError(f"duplicate bus in routes: {name}")
            seen.add(name)
            if parent is not None:
                parent = self._validate_name(parent)
            pairs.append((name, parent))

        if "master" not in seen:
            pairs.insert(0, ("master", None))
            seen.add("master")

        parents = dict(pairs)
        if parents.get("master") is not None:
            raise ValueError("master must be the root")

        for name, parent in pairs:
            if name == "master":
                continue
            if parent is None:
                raise ValueError(f"orphan bus in routes: {name}")
            if parent not in parents:
                raise ValueError(f"unknown parent in routes: {parent}")

        for name, _ in pairs:
            self._validate_route_chain(name, parents)

        return tuple(pairs)

    def _validate_route_chain(
        self, name: str, parents: dict[str, str | None]
    ) -> None:
        seen: set[str] = set()
        cursor: str | None = name
        depth = 0
        while cursor is not None:
            if cursor in seen:
                raise ValueError(f"routing cycle in routes: {name}")
            seen.add(cursor)
            if cursor != "master":
                depth += 1
            cursor = parents.get(cursor)
        if name != "master" and "master" not in seen:
            raise ValueError(f"orphan bus in routes: {name}")
        if depth > self.max_depth:
            raise ValueError(
                f"bus depth {depth} exceeds max_depth {self.max_depth}: {name}"
            )

    def _topological_order(
        self, routes: tuple[tuple[str, str | None], ...]
    ) -> list[str]:
        positions = {name: index for index, (name, _) in enumerate(routes)}
        parents = dict(routes)
        return [
            name
            for name, _ in sorted(
                routes,
                key=lambda item: (
                    self._route_depth(item[0], parents),
                    positions[item[0]],
                ),
            )
        ]

    def _route_depth(
        self, name: str, parents: dict[str, str | None]
    ) -> int:
        depth = 0
        cursor = name
        while cursor is not None and cursor != "master":
            depth += 1
            cursor = parents.get(cursor)
        return depth

    @staticmethod
    def _normalize_sends(sends) -> dict[str, dict]:
        if sends is None:
            sends = {}
        if not isinstance(sends, dict):
            raise TypeError("sends must be a mapping")
        normalized: dict[str, dict] = {}
        for target, config in sends.items():
            if not isinstance(target, str) or not target:
                raise ValueError("send target must be a non-empty string")
            if isinstance(config, dict):
                amount = config.get("amount", 0.0)
                pre_fader = config.get("pre_fader", True)
            else:
                amount = config
                pre_fader = True
            amount = AudioBusGraph._validate_amount(amount)
            normalized[target] = {
                "amount": amount,
                "pre_fader": bool(pre_fader),
            }
        return normalized

    @staticmethod
    def _validate_name(name) -> str:
        if not isinstance(name, str) or not name:
            raise ValueError("bus and voice names must be non-empty strings")
        return name

    @staticmethod
    def _validate_gain(gain) -> float:
        if not isinstance(gain, (int, float)) or not math.isfinite(gain) or gain < 0:
            raise ValueError("gain must be a finite non-negative number")
        return float(gain)

    @staticmethod
    def _validate_amount(amount) -> float:
        if (
            not isinstance(amount, (int, float))
            or not math.isfinite(amount)
            or amount < 0
        ):
            raise ValueError("send amount must be a finite non-negative number")
        return float(amount)

    def _require_bus(self, name: str) -> _Bus:
        try:
            return self._buses[name]
        except KeyError as exc:
            raise ValueError(f"unknown bus: {name}") from exc

    def _local_depth(self, name: str) -> int:
        depth = 0
        seen: set[str] = set()
        cursor = name
        while cursor is not None and cursor != "master":
            if cursor in seen:
                raise ValueError(f"routing cycle at bus: {name}")
            seen.add(cursor)
            node = self._buses.get(cursor)
            if node is None:
                raise ValueError(f"unknown parent bus: {cursor}")
            depth += 1
            cursor = node.parent
        return depth

    def _route_info(
        self, name: str
    ) -> tuple[int, bool, str | None, bool]:
        depth = 0
        seen: set[str] = set()
        cursor: str | None = name
        while cursor is not None and cursor != "master":
            if cursor in seen:
                return depth, False, None, True
            seen.add(cursor)
            node = self._buses.get(cursor)
            if node is None:
                return depth, False, cursor, False
            depth += 1
            cursor = node.parent
        return depth, cursor == "master", None, False

    def _subtree_height(self, name: str) -> int:
        children: dict[str, list[str]] = {}
        for child, node in self._buses.items():
            if node.parent is not None:
                children.setdefault(node.parent, []).append(child)
        height = 0
        stack = [(name, 0)]
        seen: set[str] = set()
        while stack:
            current, current_depth = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            height = max(height, current_depth)
            for child in children.get(current, ()):
                stack.append((child, current_depth + 1))
        return height

    def _frames_per_buffer(self) -> int:
        return max(1, round(self.sample_rate * self._buffer_seconds))

    @staticmethod
    def _channels_for_layout(layout) -> int:
        if isinstance(layout, int):
            if layout <= 0:
                raise ValueError("channel count must be positive")
            return layout
        if isinstance(layout, str):
            count = _LAYOUT_CHANNELS.get(layout.strip().lower())
            if count is not None:
                return count
            try:
                parsed = int(layout)
            except ValueError as exc:
                raise ValueError(f"unknown channel layout: {layout}") from exc
            if parsed <= 0:
                raise ValueError("channel count must be positive")
            return parsed
        raise ValueError(f"unknown channel layout: {layout!r}")

    @staticmethod
    def _layout_name(channels: int) -> str:
        for name, count in _LAYOUT_CHANNELS.items():
            if count == channels and not name[0].isdigit():
                return name
        return f"{channels}.0"

    @staticmethod
    def _channel_names(channels: int) -> tuple[str, ...]:
        if channels in _LAYOUT_NAMES:
            return _LAYOUT_NAMES[channels]
        return tuple(f"Ch{index + 1}" for index in range(channels))
