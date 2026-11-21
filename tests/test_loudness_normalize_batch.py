import pytest

import loudness_normalize_batch as lnb


def item(name, lufs, peak_db=-6.0, duration=10.0, layout="stereo", silent=False):
    return {
        "name": name,
        "lufs": lufs,
        "peak_db": peak_db,
        "duration": duration,
        "layout": layout,
        "silent": silent,
    }


def test_absolute_gate_treats_all_silence_as_no_measurement():
    batch = lnb.LoudnessBatch()
    assert batch.integrated([{"lufs": -80.0, "seconds": 10.0}]) is None
    result = batch.plan([item("silent.wav", -80.0, silent=True)])
    entry = result["items"][0]
    assert entry["gain_db"] == 0.0
    assert entry["skip"] is True
    assert entry["reason"] == "silent"


def test_relative_gate_ignores_quiet_blocks():
    batch = lnb.LoudnessBatch()
    blocks = [
        {"lufs": -20.0, "seconds": 1.0},
        {"lufs": -20.0, "seconds": 1.0},
        {"lufs": -20.0, "seconds": 1.0},
        {"lufs": -60.0, "seconds": 1.0},
    ]
    assert batch.integrated(blocks) == pytest.approx(-20.0, abs=0.01)


def test_silence_padding_does_not_inflate_gain():
    batch = lnb.LoudnessBatch()
    blocks = [{"lufs": -23.0, "seconds": 1.0} for _ in range(5)]
    blocks += [{"lufs": -80.0, "seconds": 1.0} for _ in range(20)]
    assert batch.integrated(blocks) == pytest.approx(-23.0, abs=0.01)
    assert batch.gain_for(-23.0) == pytest.approx(7.0)


def test_five_one_channels_do_not_count_lfe_twice():
    batch = lnb.LoudnessBatch()
    assert batch.channel_weight("mono") == [1.0]
    assert batch.channel_weight("stereo") == [1.0, 1.0]
    weights = batch.channel_weight("5.1")
    assert weights == [1.0, 1.0, 1.0, 0.0, 1.0, 1.0]
    assert sum(weights) == 5.0


def test_five_one_is_corrected_below_stereo():
    batch = lnb.LoudnessBatch()
    result = batch.plan([item("st.wav", -20.0), item("51.wav", -20.0, layout="5.1")])
    stereo = result["items"][0]
    surround = result["items"][1]
    assert stereo["gain_db"] == pytest.approx(4.0)
    assert surround["gain_db"] - stereo["gain_db"] == pytest.approx(0.79, abs=0.02)


def test_true_peak_limit_prevents_clipping():
    batch = lnb.LoudnessBatch(true_peak_limit=-1.0)
    assert batch.gain_for(-30.0, peak_db=-0.5) == pytest.approx(-0.5)


def test_file_already_at_target_is_not_gained_again():
    batch = lnb.LoudnessBatch(target_lufs=-16.0, tolerance=0.1)
    assert batch.gain_for(-16.05) == 0.0
    entry = batch.plan([item("ok.wav", -16.05)])["items"][0]
    assert entry["skip"] is True
    assert entry["reason"] == "on_target"


def test_negative_target_keeps_sign():
    batch = lnb.LoudnessBatch()
    assert batch.gain_for(-20.0, target_lufs=-23.0) == pytest.approx(-3.0)


def test_short_clip_uses_stable_strategy():
    batch = lnb.LoudnessBatch(window_seconds=0.4)
    entry = batch.plan([item("short.wav", -30.0, duration=0.2)])["items"][0]
    assert entry["stable"] is False
    assert entry["skip"] is True
    assert entry["reason"] == "too_short"


def test_integration_uses_time_not_block_count():
    batch = lnb.LoudnessBatch()
    short = [{"lufs": -20.0, "seconds": 0.5} for _ in range(4)]
    long = [{"lufs": -20.0, "seconds": 2.0}]
    assert batch.integrated(short) == pytest.approx(batch.integrated(long), abs=0.01)


def test_album_mode_applies_one_gain_to_all_tracks():
    batch = lnb.LoudnessBatch(target_lufs=-16.0)
    result = batch.plan([item("a.wav", -20.0), item("b.wav", -10.0)], mode="album")
    gains = [entry["gain_db"] for entry in result["items"]]
    assert gains[0] == pytest.approx(gains[1])
    assert result["album_gain_db"] == pytest.approx(-3.4, abs=0.05)


def test_single_mode_uses_per_track_gain():
    batch = lnb.LoudnessBatch(target_lufs=-16.0)
    result = batch.plan([item("a.wav", -20.0), item("b.wav", -10.0)], mode="single")
    gains = [entry["gain_db"] for entry in result["items"]]
    assert gains[0] == pytest.approx(4.0)
    assert gains[1] == pytest.approx(-6.0)


def test_output_names_do_not_overwrite_each_other():
    batch = lnb.LoudnessBatch()
    assert batch.output_names(["a.wav", "a.wav", "b.wav"]) == [
        "a.wav",
        "a__1.wav",
        "b.wav",
    ]


def test_metadata_and_cover_survive_write_back():
    batch = lnb.LoudnessBatch()
    original = {"tags": {"artist": "x", "album": "z"}, "cover": b"img"}
    merged = batch.merge_metadata(original, {"tags": {"title": "y"}})
    assert merged["tags"]["artist"] == "x"
    assert merged["tags"]["album"] == "z"
    assert merged["tags"]["title"] == "y"
    assert merged["cover"] == b"img"


def test_checkpoint_and_resume_skip_finished_files():
    batch = lnb.LoudnessBatch()
    state = batch.checkpoint(["a.wav", "b.wav", "c.wav"], 10)
    assert state == {"done": 3, "total": 10, "remaining": 7, "complete": False}
    items = [item("a.wav", -20.0), item("b.wav", -20.0), item("c.wav", -20.0), item("d.wav", -20.0)]
    rest = batch.resume(items, ["a.wav", "b.wav", "c.wav"])
    assert [entry["name"] for entry in rest] == ["d.wav"]
