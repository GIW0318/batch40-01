import pytest

import color_matrix_convert as cmc


def test_bt709_coefficients():
    conv = cmc.ColorMatrixConverter()
    coeff = conv.coefficients("bt709")
    assert coeff["kr"] == pytest.approx(0.2126, abs=1e-4)
    assert coeff["kg"] == pytest.approx(0.7152, abs=1e-4)
    assert coeff["kb"] == pytest.approx(0.0722, abs=1e-4)


def test_bt601_coefficients():
    conv = cmc.ColorMatrixConverter()
    coeff = conv.coefficients("bt601")
    assert coeff["kr"] == pytest.approx(0.299, abs=1e-4)
    assert coeff["kg"] == pytest.approx(0.587, abs=1e-4)
    assert coeff["kb"] == pytest.approx(0.114, abs=1e-4)


def test_matrix_is_inferred_from_resolution():
    conv = cmc.ColorMatrixConverter()
    assert conv.infer_matrix(1280, 720) == "bt709"
    assert conv.infer_matrix(720, 480) == "bt601"


def test_stream_metadata_overrides_resolution_guess():
    conv = cmc.ColorMatrixConverter()
    assert conv.infer_matrix(720, 480, {"matrix": "bt709"}) == "bt709"
    assert conv.infer_matrix(1280, 720, {"matrix": "bt601"}) == "bt601"


def test_limited_range_is_expanded_to_full_range():
    conv = cmc.ColorMatrixConverter()
    assert conv.range_scale(16, "limited", "full") == 0
    assert conv.range_scale(235, "limited", "full") == 255
    assert conv.range_scale(126, "limited", "full") == 128
    assert conv.range_scale(0, "full", "limited") == 16
    assert conv.range_scale(255, "full", "limited") == 235


def test_red_and_blue_are_not_swapped():
    conv = cmc.ColorMatrixConverter()
    red = conv.rgb_to_yuv((255, 0, 0), "bt709")
    blue = conv.rgb_to_yuv((0, 0, 255), "bt709")
    assert red["y"] == pytest.approx(54.2, abs=0.5)
    assert red["v"] > 200
    assert red["u"] < 128
    assert blue["u"] > 200
    assert blue["v"] < 128


def test_odd_dimensions_round_chroma_up():
    conv = cmc.ColorMatrixConverter()
    assert conv.chroma_size(3, 3) == (2, 2)
    assert conv.chroma_size(5, 3) == (3, 2)
    assert conv.chroma_size(4, 2) == (2, 1)


def test_nv12_and_i420_layouts_are_distinct():
    conv = cmc.ColorMatrixConverter()
    nv12 = conv.plane_layout("nv12", 4, 2)
    i420 = conv.plane_layout("i420", 4, 2)
    assert nv12["interleaved"] is True
    assert nv12["uv"] == 4
    assert nv12["u"] == 0
    assert i420["interleaved"] is False
    assert i420["u"] == 2
    assert i420["v"] == 2


def test_chroma_siting_offsets():
    conv = cmc.ColorMatrixConverter()
    assert conv.chroma_offset("co-sited") == (0.0, 0.0)
    assert conv.chroma_offset("centered") == (0.5, 0.5)


def test_hdr_tone_map_keeps_highlight_detail():
    conv = cmc.ColorMatrixConverter()
    assert conv.tone_map(0.0) == 0.0
    assert conv.tone_map(1.0) >= 0.8
    assert conv.tone_map(1.0) <= 1.0
    assert conv.tone_map(0.5) > conv.tone_map(0.2)


def test_premultiplied_and_straight_alpha_agree():
    conv = cmc.ColorMatrixConverter()
    straight = conv.alpha_blend({"r": 100, "g": 40, "b": 20, "a": 0.5, "premultiplied": False})
    premultiplied = conv.alpha_blend({"r": 50, "g": 20, "b": 10, "a": 0.5, "premultiplied": True})
    assert straight == premultiplied
    assert straight["r"] == 50
    assert straight["g"] == 20
    assert straight["b"] == 10


def test_conversion_chain_rounds_only_once():
    conv = cmc.ColorMatrixConverter()
    single = conv.convert_chain((123, 45, 200), steps=1)
    many = conv.convert_chain((123, 45, 200), steps=4)
    assert many == single


def test_tile_ranges_cover_every_pixel_once():
    conv = cmc.ColorMatrixConverter()
    ranges = conv.tile_ranges(13, 7, 5)
    seen = set()
    for x0, x1, y0, y1 in ranges:
        for y in range(y0, y1):
            for x in range(x0, x1):
                assert (x, y) not in seen
                seen.add((x, y))
    assert len(seen) == 13 * 7


def test_simd_tail_does_not_read_past_frame():
    conv = cmc.ColorMatrixConverter()
    chunks = conv.process_chunks(13, lanes=4)
    assert chunks[0][0] == 0
    assert chunks[-1][1] == 13
    assert chunks[-1][1] - chunks[-1][0] == 1
    assert all(end - start <= 4 for start, end in chunks)
    assert all(chunks[i][1] == chunks[i + 1][0] for i in range(len(chunks) - 1))


def test_unknown_matrix_and_range_are_rejected():
    conv = cmc.ColorMatrixConverter()
    with pytest.raises(ValueError):
        conv.coefficients("unknown")
    with pytest.raises(ValueError):
        conv.range_scale(16, "limited", "unknown")
