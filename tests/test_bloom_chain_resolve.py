import math

import pytest

import bloom_chain_resolve as bcr


def service():
    return bcr.BloomChainResolve(threshold=1.0, intensity=1.0, exposure=1.0)


def test_srgb_to_linear_known_values():
    chain = service()
    assert chain.srgb_to_linear(0.0) == pytest.approx(0.0)
    assert chain.srgb_to_linear(0.04045) == pytest.approx(0.04045 / 12.92)
    assert chain.srgb_to_linear(0.5) == pytest.approx(0.214041, rel=1e-4)
    assert chain.srgb_to_linear(1.0) == pytest.approx(1.0)


def test_extract_thresholds_in_linear_space():
    chain = service()
    assert chain.extract([0.8], 1, 1, threshold=0.7) == [0.0]
    assert chain.extract([1.0], 1, 1, threshold=0.7)[0] > 0.0


def test_exposure_is_applied_before_threshold():
    chain = service()
    assert chain.extract([0.8], 1, 1, threshold=0.7)[0] == 0.0
    assert chain.extract(
        [0.8], 1, 1, threshold=0.7, exposure=2.0
    )[0] > 0.0


def test_extract_contains_nan_and_infinity():
    chain = service()
    result = chain.extract(
        [float("nan"), float("inf"), 0.5], 3, 1, threshold=0.1
    )
    assert result[0] == 0.0
    assert result[1] == 0.0
    assert result[2] > 0.0
    assert all(math.isfinite(value) for value in result)


def test_downsample_odd_size_keeps_last_row_and_column():
    chain = service()
    out, width, height = chain.downsample([1.0] * 9, 3, 3)
    assert (width, height) == (2, 2)
    assert out == pytest.approx([1.0, 1.0, 1.0, 1.0])


def test_downsample_odd_edge_cell_keeps_energy():
    chain = service()
    out, width, height = chain.downsample([0.0, 0.0, 1.0], 3, 1)
    assert (width, height) == (2, 1)
    assert out == pytest.approx([0.0, 1.0])


def test_upsample_add_does_not_double_the_base():
    chain = service()
    assert chain.upsample_add([1.0], [1.0], 1, 1) == pytest.approx([2.0])


def test_build_chain_halves_with_ceil():
    chain = service()
    assert chain.build_chain(5, 3) == [(5, 3), (3, 2), (2, 1), (1, 1)]
    assert chain.build_chain(1, 1) == [(1, 1)]


def test_resolution_change_rebuilds_chain():
    chain = service()
    small = chain.resolve([1.0] * 16, 4, 4)
    large = chain.resolve([1.0] * 64, 8, 8)
    assert small["sizes"][0] == (4, 4)
    assert large["sizes"][0] == (8, 8)
    assert large["sizes"] != small["sizes"]


def test_zero_intensity_skips_chain():
    chain = service()
    result = chain.resolve([1.0], 1, 1, intensity=0.0)
    assert result["skipped"] is True
    assert result["sizes"] == []
    assert result["output"] == [1.0]


def test_resolve_adds_bloom_to_bright_pixels():
    chain = service()
    result = chain.resolve(
        [1.0], 1, 1, intensity=1.0, threshold=0.5, exposure=1.0
    )
    assert result["skipped"] is False
    assert result["output"][0] > 1.0
    assert math.isfinite(result["output"][0])


def test_repeated_resolve_reuses_targets():
    chain = service()
    chain.resolve([1.0] * 4, 2, 2)
    before = chain.allocations("main")
    assert before > 0
    chain.resolve([1.0] * 4, 2, 2)
    assert chain.allocations("main") == before


def test_camera_cut_clears_temporal_history():
    chain = service()
    chain.resolve([1.0], 1, 1)
    chain.resolve([1.0], 1, 1)
    assert chain.history_size("main") == 2
    assert chain.camera_cut() == 2
    assert chain.history_size("main") == 0


def test_zero_intensity_allocates_nothing():
    chain = service()
    chain.resolve([1.0], 1, 1, intensity=0.0)
    assert chain.allocations("main") == 0


def test_targets_keep_separate_outputs():
    chain = service()
    main = chain.resolve([1.0, 1.0], 2, 1, target="main", threshold=0.5)
    ui = chain.resolve([0.0, 0.0], 2, 1, target="ui", threshold=0.5)
    assert all(value > 0.0 for value in main["output"])
    assert ui["output"] == [0.0, 0.0]
