import pytest

import spatial_occlusion_mix as som


def one_source_mixer():
    mixer = som.OcclusionMixer(head_radius=0.5)
    mixer.set_listener((0.0, 0.0), forward=(0.0, 1.0))
    mixer.add_source("footstep", position=(0.0, 10.0))
    return mixer


def test_partial_occlusion_uses_multiple_rays():
    mixer = one_source_mixer()
    mixer.add_wall("center", start=(-0.05, 5.0), end=(0.05, 5.0))
    mixer.begin_frame()
    assert mixer.occlusion("footstep") == pytest.approx(1.0 / 3.0)


def test_more_blocked_rays_increase_occlusion():
    mixer = one_source_mixer()
    mixer.add_wall("center", start=(-0.05, 5.0), end=(0.05, 5.0))
    mixer.add_wall("left", start=(-0.3, 4.0), end=(-0.1, 4.0))
    mixer.begin_frame()
    assert mixer.occlusion("footstep") == pytest.approx(2.0 / 3.0)


def test_closed_door_blocks_sound():
    mixer = one_source_mixer()
    mixer.add_wall("doorway", start=(-1.0, 5.0), end=(1.0, 5.0), door="gate")
    mixer.set_door("gate", True)
    mixer.begin_frame()
    assert mixer.occlusion("footstep") == pytest.approx(1.0)


def test_open_door_lets_sound_through():
    mixer = one_source_mixer()
    mixer.add_wall("doorway", start=(-1.0, 5.0), end=(1.0, 5.0), door="gate")
    mixer.set_door("gate", False)
    mixer.begin_frame()
    assert mixer.occlusion("footstep") == pytest.approx(0.0)


def test_floor_separates_floors():
    mixer = one_source_mixer()
    mixer.add_floor("slab", y=2.0)
    mixer.begin_frame()
    assert mixer.occlusion("footstep") == pytest.approx(1.0)


def test_ray_budget_limits_sources_per_frame():
    mixer = som.OcclusionMixer(ray_budget=4)
    mixer.set_listener((0.0, 0.0))
    for index in range(10):
        mixer.add_source("s%d" % index, position=(0.0, 10.0), importance=float(index))
    mixer.begin_frame()
    assert mixer.ray_budget_used() == 4


def test_deferred_sources_reported():
    mixer = som.OcclusionMixer(ray_budget=4)
    mixer.set_listener((0.0, 0.0))
    for index in range(10):
        mixer.add_source("s%d" % index, position=(0.0, 10.0), importance=float(index))
    mixer.begin_frame()
    assert mixer.stats()["deferred_sources"] == 6


def test_doppler_uses_current_listener_velocity():
    mixer = som.OcclusionMixer()
    mixer.add_source("car", position=(0.0, 10.0))
    mixer.set_listener((0.0, 0.0))
    mixer.begin_frame(dt=0.1)
    mixer.set_listener((0.0, 2.0))
    mixer.begin_frame(dt=0.1)
    assert mixer.doppler("car", 0.1) > 1.0


def test_attenuation_is_continuous():
    mixer = som.OcclusionMixer()
    mixer.set_listener((0.0, 0.0))
    mixer.add_source("a", position=(0.0, 10.0))
    mixer.add_source("b", position=(0.0, 10.02))
    first = mixer.attenuation("a")
    second = mixer.attenuation("b")
    assert abs(first - second) < 0.001


def test_reverb_boundary_is_smooth():
    mixer = som.OcclusionMixer()
    mixer.add_reverb_zone("hall", center=(0.0, 0.0), radius=5.0, wet=1.0)
    assert mixer.reverb_at((0.0, 0.0)) == pytest.approx(1.0)
    inside = mixer.reverb_at((4.99, 0.0))
    outside = mixer.reverb_at((5.01, 0.0))
    assert abs(inside - outside) < 0.05


def test_listener_snapshot_is_stable_within_frame():
    mixer = som.OcclusionMixer()
    mixer.add_source("steps", position=(-5.0, 5.0))
    mixer.set_listener((0.0, 0.0), forward=(0.0, 1.0))
    mixer.begin_frame()
    first = mixer.pan("steps")
    mixer.set_listener((0.0, 0.0), forward=(-1.0, 0.0))
    second = mixer.pan("steps")
    assert second == pytest.approx(first)
    mixer.begin_frame()
    assert mixer.pan("steps") > 0.0


def test_pan_follows_source_side():
    mixer = som.OcclusionMixer()
    mixer.set_listener((0.0, 0.0), forward=(0.0, 1.0))
    mixer.add_source("left", position=(-5.0, 5.0))
    mixer.add_source("right", position=(5.0, 5.0))
    mixer.begin_frame()
    assert mixer.pan("left") < 0.0
    assert mixer.pan("right") > 0.0


def test_mono_head_radius_creates_interaural_delay():
    mixer = som.OcclusionMixer(head_radius=0.5)
    mixer.set_listener((0.0, 0.0), forward=(0.0, 1.0))
    mixer.add_source("side", position=(2.0, 0.0))
    mixer.add_source("front", position=(0.0, 2.0))
    mixer.begin_frame()
    assert mixer.interaural_delay("side") > 0.0
    assert mixer.interaural_delay("front") < 0.000001


def test_far_sources_leave_mix():
    mixer = som.OcclusionMixer(max_distance=50.0)
    mixer.set_listener((0.0, 0.0))
    mixer.add_source("near", position=(0.0, 10.0))
    mixer.add_source("far", position=(0.0, 60.0))
    assert mixer.audible() == ["near"]


def test_frame_snapshot_is_deterministic_for_door_and_move():
    mixer = som.OcclusionMixer()
    mixer.add_source("guard", position=(0.0, 10.0))
    mixer.set_listener((0.0, 0.0))
    mixer.add_wall("doorway", start=(-1.0, 5.0), end=(1.0, 5.0), door="gate")
    mixer.set_door("gate", True)
    mixer.begin_frame()
    assert mixer.occlusion("guard") == pytest.approx(1.0)
    mixer.set_door("gate", False)
    mixer.set_listener((10.0, 10.0))
    assert mixer.occlusion("guard") == pytest.approx(1.0)
    mixer.begin_frame()
    assert mixer.occlusion("guard") == pytest.approx(0.0)
