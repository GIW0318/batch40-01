import math

import pytest

import reflection_probe_blend as rpb


def two_probes():
    probes = rpb.ReflectionProbeSet()
    probes.add_probe("a", center=(0.0, 0.0), size=(4.0, 4.0))
    probes.add_probe("b", center=(2.0, 0.0), size=(4.0, 4.0))
    return probes


def test_box_projection_sign_matches_point_direction():
    probes = rpb.ReflectionProbeSet()
    probes.add_probe("p", center=(0.0, 0.0), size=(4.0, 4.0))
    assert probes.box_projection("p", (1.0, 0.0))[0] == pytest.approx(0.5)
    assert probes.box_projection("p", (-1.0, 0.0))[0] == pytest.approx(-0.5)


def test_blend_weights_are_continuous_across_boundary():
    probes = two_probes()
    left = probes.weights((0.9, 0.0))["a"]
    middle = probes.weights((1.0, 0.0))["a"]
    right = probes.weights((1.1, 0.0))["a"]
    assert abs(left - middle) < 0.15
    assert abs(middle - right) < 0.15


def test_unbaked_probe_not_blended():
    probes = rpb.ReflectionProbeSet()
    probes.add_probe("p", baked=False)
    assert probes.weights((0.0, 0.0)) == {}


def test_baked_probe_participates():
    probes = rpb.ReflectionProbeSet()
    probes.add_probe("p", baked=False)
    probes.mark_baked("p")
    assert probes.weights((0.0, 0.0))["p"] == pytest.approx(1.0)


def test_mip_matches_roughness():
    probes = rpb.ReflectionProbeSet()
    probes.add_probe("p")
    assert probes.sample_mip("p", 0.0) == 0
    assert probes.sample_mip("p", 1.0) == 7
    assert probes.sample_mip("p", 0.25) <= probes.sample_mip("p", 0.75)


def test_obb_bounds_respect_rotation():
    probes = rpb.ReflectionProbeSet()
    probes.add_probe("corridor", center=(0.0, 0.0), size=(4.0, 0.5), rotation=math.pi / 4.0)
    assert probes.bounds_contain("corridor", (1.2, 1.2)) is True
    assert probes.bounds_contain("corridor", (1.5, 1.5)) is False


def test_equal_weights_have_stable_order():
    first = rpb.ReflectionProbeSet()
    first.add_probe("b", center=(1.0, 0.0), size=(4.0, 4.0))
    first.add_probe("a", center=(-1.0, 0.0), size=(4.0, 4.0))
    second = rpb.ReflectionProbeSet()
    second.add_probe("a", center=(-1.0, 0.0), size=(4.0, 4.0))
    second.add_probe("b", center=(1.0, 0.0), size=(4.0, 4.0))
    assert [item[0] for item in first.ordered_weights((0.0, 0.0))] == ["a", "b"]
    assert first.ordered_weights((0.0, 0.0)) == second.ordered_weights((0.0, 0.0))


def test_invalid_probe_fades_smoothly():
    probes = rpb.ReflectionProbeSet()
    probes.add_probe("p")
    probes.set_invalid("p")
    factors = []
    for _ in range(4):
        probes.tick()
        factors.append(probes.invalid_factor("p"))
    assert factors == pytest.approx([0.75, 0.5, 0.25, 0.0])
    assert probes.invalid_factor("p") == 0.0


def test_dynamic_bakes_spread_over_ticks():
    probes = rpb.ReflectionProbeSet(bakes_per_tick=1)
    for name in ["d1", "d2", "d3"]:
        probes.add_probe(name, baked=False, dynamic=True)
    assert probes.tick() == ["d1"]
    assert probes.tick() == ["d2"]
    assert probes.tick() == ["d3"]
    assert probes.tick() == []


def test_recursion_depth_is_capped():
    probes = rpb.ReflectionProbeSet(max_reflection_depth=2)
    assert probes.recursion_allowed(2) is True
    assert probes.recursion_allowed(3) is False


def test_reflection_camera_excludes_own_probe():
    probes = rpb.ReflectionProbeSet()
    probes.add_probe("a")
    probes.add_probe("b")
    assert probes.probes_for_camera("a") == ["b"]


def test_face_size_clamped_on_mobile():
    probes = rpb.ReflectionProbeSet(max_face_size=256)
    probes.add_probe("mobile", face_size=1024)
    assert probes.face_size("mobile") == 256
    assert probes.stats()["size_clamps"] == 1


def test_weights_sum_to_one():
    probes = two_probes()
    weights = probes.weights((1.0, 0.0))
    assert sum(weights.values()) == pytest.approx(1.0)


def test_out_of_bounds_point_has_no_weights():
    probes = two_probes()
    assert probes.weights((100.0, 100.0)) == {}


def test_removed_probe_not_in_weights():
    probes = two_probes()
    probes.remove_probe("a")
    assert "a" not in probes.weights((0.0, 0.0))
