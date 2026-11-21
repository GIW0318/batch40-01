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

    def add_state(self, name, duration, loop=True, events=None):
        raise NotImplementedError

    def play(self, name, phase=0.0):
        raise NotImplementedError

    def transition_to(self, name, duration=0.25, interrupt=False):
        raise NotImplementedError

    def update(self, dt):
        raise NotImplementedError

    def remap_phase(self, phase, from_duration, to_duration):
        raise NotImplementedError

    def bone_allowed(self, mask, bone):
        raise NotImplementedError

    def blend_root_motion(self, motions, weights):
        raise NotImplementedError

    def blend_weight_at(self, duration, elapsed):
        raise NotImplementedError

    def combine_layer_weights(self, pairs):
        raise NotImplementedError

    def hot_reload(self):
        raise NotImplementedError

    def queue_size(self):
        raise NotImplementedError
