"""Animation graph transition controller.

Contract used by the tests
--------------------------
* ``__init__(max_queue=4)``.
* ``add_state(name, duration, loop=True, events=None)``: events is a list of
  (phase, name) with 0 <= phase <= 1.
* ``play(name, phase=0.0)`` sets the current state with no transition.
* ``transition_to(name, duration=0.25, interrupt=False)``:
  - no active transition: starts one and returns {"source", "target",
    "duration", "source_phase", "queued": False} where source_phase is the
    current state phase;
  - active transition and interrupt=True: switches the source to the current
    target at its current phase (continued, not reset) and returns it as
    source_phase with queued False;
  - otherwise queues the target (deduplicated) up to max_queue.  A duplicate
    or full queue returns {"queued": False}; success returns {"queued":
    True}.
* ``update(dt)`` advances the active transition.  Weight and phases are
  linear in elapsed/duration.  Events fire from the source interval
  (source_phase_at_start, source_phase_now] and the target interval
  (target_phase_at_start, target_phase_now]; a name is reported at most once
  per update even when both ends contain it.  Returns {"state", "weight",
  "transition_active", "events"}.  The state is the source until the
  transition completes, then the target.
* A duration 0 transition completes immediately: update returns weight 1.0
  and the target state, with no ZeroDivisionError.
* ``remap_phase(phase, from_duration, to_duration)`` returns
  phase * to_duration; the source duration is irrelevant.
* ``bone_allowed(mask, bone)``: "upper" allows spine, arm and hand bones but
  not any "finger_*" bone; "full" allows every bone.
* ``blend_root_motion(motions, weights)`` returns sum(motion * weight);
  weights are not assumed to sum to 1.
* ``blend_weight_at(duration, elapsed)`` returns 0.0 before the start, 1.0
  when duration is 0, else min(1.0, elapsed/duration).
* ``combine_layer_weights(pairs)`` normalizes (layer, weight) pairs so the
  returned dict sums to 1.0; a zero total raises ValueError.
* ``hot_reload()`` invalidates any active transition; update then raises
  RuntimeError until a new transition_to is called.
* ``queue_size()`` returns the number of queued targets.
"""


class AnimGraphTransition:
    def __init__(self, max_queue=4):
        self.max_queue = max_queue
        self._states = {}
        self._current = None
        self._phase = 0.0
        self._transition = None
        self._queue = []
        self._invalid = False

    def add_state(self, name, duration, loop=True, events=None):
        self._states[name] = {
            "duration": float(duration),
            "loop": loop,
            "events": list(events or []),
        }

    def play(self, name, phase=0.0):
        self._require_state(name)
        self._invalid = False
        self._current = name
        self._phase = float(phase)
        self._transition = None
        self._queue = []

    def transition_to(self, name, duration=0.25, interrupt=False):
        if self._transition is not None:
            if interrupt:
                active = self._transition
                continued_phase = active["tgt_last"]
                self._transition = self._make_transition(
                    active["target"], continued_phase, name, duration
                )
                return {
                    "source": active["target"],
                    "target": name,
                    "duration": duration,
                    "source_phase": continued_phase,
                    "queued": False,
                }
            if name == self._transition["target"] or name in self._queue:
                return {"queued": False}
            if len(self._queue) >= self.max_queue:
                return {"queued": False}
            self._queue.append(name)
            return {"queued": True}

        self._require_state(name)
        self._invalid = False
        self._queue = []
        self._transition = self._make_transition(
            self._current, self._phase, name, duration
        )
        return {
            "source": self._current,
            "target": name,
            "duration": duration,
            "source_phase": self._phase,
            "queued": False,
        }

    def update(self, dt):
        if self._invalid:
            raise RuntimeError(
                "animation graph was hot-reloaded; pending transitions are invalid"
            )

        active = self._transition
        if active is None:
            return {
                "state": self._current,
                "weight": 1.0,
                "transition_active": False,
                "events": [],
            }

        active["elapsed"] += dt

        if active["duration"] <= 0.0:
            self._current = active["target"]
            self._phase = active["tgt_last"]
            self._transition = None
            return {
                "state": active["target"],
                "weight": 1.0,
                "transition_active": False,
                "events": [],
            }

        src_now = active["src_last"] + dt / self._state_duration(active["source"])
        tgt_now = active["tgt_last"] + dt / self._state_duration(active["target"])
        events = self._collect_events(active, src_now, tgt_now)
        active["src_last"] = src_now
        active["tgt_last"] = tgt_now

        weight = min(1.0, active["elapsed"] / active["duration"])
        if active["elapsed"] >= active["duration"]:
            self._current = active["target"]
            self._phase = tgt_now
            self._transition = None
            return {
                "state": active["target"],
                "weight": 1.0,
                "transition_active": False,
                "events": events,
            }

        return {
            "state": active["source"],
            "weight": weight,
            "transition_active": True,
            "events": events,
        }

    def remap_phase(self, phase, from_duration, to_duration):
        return phase * to_duration

    def bone_allowed(self, mask, bone):
        if mask == "full":
            return True
        if mask == "upper":
            if bone.startswith("finger_"):
                return False
            return any(part in bone for part in ("spine", "arm", "hand"))
        return False

    def blend_root_motion(self, motions, weights):
        return sum(motion * weight for motion, weight in zip(motions, weights))

    def blend_weight_at(self, duration, elapsed):
        if elapsed < 0.0:
            return 0.0
        if duration <= 0.0:
            return 1.0
        return min(1.0, elapsed / duration)

    def combine_layer_weights(self, pairs):
        total = sum(weight for _, weight in pairs)
        if total <= 0.0:
            raise ValueError("combined layer weights must have a positive total")
        return {layer: weight / total for layer, weight in pairs}

    def hot_reload(self):
        self._transition = None
        self._queue = []
        self._invalid = True

    def queue_size(self):
        return len(self._queue)

    def _make_transition(self, source, source_phase, target, duration):
        self._require_state(target)
        return {
            "source": source,
            "target": target,
            "duration": float(duration),
            "elapsed": 0.0,
            "src_start": source_phase,
            "src_last": source_phase,
            "tgt_start": 0.0,
            "tgt_last": 0.0,
        }

    def _collect_events(self, active, src_now, tgt_now):
        fired = []
        seen = set()

        def scan(state_name, last, now):
            for phase, event_name in self._states[state_name]["events"]:
                if last < phase <= now and event_name not in seen:
                    seen.add(event_name)
                    fired.append(event_name)

        scan(active["source"], active["src_last"], src_now)
        scan(active["target"], active["tgt_last"], tgt_now)
        return fired

    def _state_duration(self, name):
        return self._states[name]["duration"]

    def _require_state(self, name):
        if name not in self._states:
            raise KeyError(name)
