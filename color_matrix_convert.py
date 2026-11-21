"""Color matrix, range and chroma conversion helpers."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence


class ColorMatrixConverter:
    """Helpers for the color-related stages of a YUV conversion pipeline.

    The public methods deliberately return plain Python values so the
    conversion math can be reused without pulling in an image-processing
    dependency. Intermediate calculations stay in floating point; integer
    quantization only happens when a method returns a final sample.
    """

    _MATRIX_COEFFICIENTS = {
        "bt709": (0.2126, 0.7152, 0.0722),
        "bt601": (0.2990, 0.5870, 0.1140),
        "bt2020": (0.2627, 0.6780, 0.0593),
        "smpte240m": (0.2120, 0.7010, 0.0870),
        "fcc": (0.3000, 0.5900, 0.1100),
    }

    _MATRIX_ALIASES = {
        "1": "bt709",
        "bt709": "bt709",
        "bt_709": "bt709",
        "rec709": "bt709",
        "rec_709": "bt709",
        "itu709": "bt709",
        "itu_r_bt_709": "bt709",
        "itu_r_bt709": "bt709",
        "5": "bt601",
        "6": "bt601",
        "bt601": "bt601",
        "bt_601": "bt601",
        "rec601": "bt601",
        "rec_601": "bt601",
        "itu601": "bt601",
        "itu_r_bt_601": "bt601",
        "itu_r_bt601": "bt601",
        "bt470bg": "bt601",
        "smpte170m": "bt601",
        "9": "bt2020",
        "10": "bt2020",
        "bt2020": "bt2020",
        "bt_2020": "bt2020",
        "bt2020nc": "bt2020",
        "bt2020c": "bt2020",
        "rec2020": "bt2020",
        "rec_2020": "bt2020",
        "7": "smpte240m",
        "smpte240m": "smpte240m",
        "4": "fcc",
        "fcc": "fcc",
    }

    _RANGE_ALIASES = {
        "limited": "limited",
        "tv": "limited",
        "video": "limited",
        "mpeg": "limited",
        "narrow": "limited",
        "full": "full",
        "pc": "full",
        "jpeg": "full",
        "data": "full",
        "wide": "full",
    }

    _CHROMA_SITING = {
        "co-sited": (0.0, 0.0),
        "co_sited": (0.0, 0.0),
        "cosited": (0.0, 0.0),
        "left": (0.0, 0.0),
        "topleft": (0.0, 0.0),
        "top-left": (0.0, 0.0),
        "top_left": (0.0, 0.0),
        "mpeg2": (0.0, 0.0),
        "centered": (0.5, 0.5),
        "centre": (0.5, 0.5),
        "center": (0.5, 0.5),
        "jpeg": (0.5, 0.5),
        "center-left": (0.0, 0.5),
        "center_left": (0.0, 0.5),
        "left-center": (0.0, 0.5),
        "left_center": (0.0, 0.5),
        "top": (0.5, 0.0),
        "bottom-left": (0.0, 1.0),
        "bottom_left": (0.0, 1.0),
        "bottom": (0.5, 1.0),
    }

    def __init__(self, src_matrix="bt709", src_range="limited", dst_matrix="bt709",
                 dst_range="full", alpha_mode="straight"):
        self.src_matrix = self._normalize_matrix(src_matrix)
        self.dst_matrix = self._normalize_matrix(dst_matrix)
        self.src_range = self._normalize_range(src_range)
        self.dst_range = self._normalize_range(dst_range)
        self.alpha_mode = self._normalize_alpha_mode(alpha_mode)

    @staticmethod
    def _normalize_token(value) -> str:
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, int):
            return str(value)
        if not isinstance(value, str):
            raise ValueError(f"unsupported color metadata value: {value!r}")
        return value.strip().lower().replace(".", "").replace(" ", "_").replace("-", "_")

    @classmethod
    def _normalize_matrix(cls, matrix) -> str:
        token = cls._normalize_token(matrix)
        try:
            return cls._MATRIX_ALIASES[token]
        except KeyError as exc:
            raise ValueError(f"unknown color matrix: {matrix!r}") from exc

    @classmethod
    def _normalize_range(cls, value) -> str:
        token = cls._normalize_token(value)
        try:
            return cls._RANGE_ALIASES[token]
        except KeyError as exc:
            raise ValueError(f"unknown color range: {value!r}") from exc

    @staticmethod
    def _normalize_alpha_mode(mode) -> str:
        token = str(mode).strip().lower().replace("-", "_")
        aliases = {
            "straight": "straight",
            "unpremultiplied": "straight",
            "non_premultiplied": "straight",
            "premultiplied": "premultiplied",
            "premul": "premultiplied",
        }
        try:
            return aliases[token]
        except KeyError as exc:
            raise ValueError(f"unknown alpha mode: {mode!r}") from exc

    @staticmethod
    def _positive_int(value, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return value

    @staticmethod
    def _round_half_up(value: float) -> int:
        return int(math.floor(value + 0.5))

    def coefficients(self, matrix=None):
        matrix_name = self.src_matrix if matrix is None else self._normalize_matrix(matrix)
        kr, kg, kb = self._MATRIX_COEFFICIENTS[matrix_name]
        return {"kr": kr, "kg": kg, "kb": kb}

    def infer_matrix(self, width, height, metadata=None):
        """Resolve the matrix, preferring explicit stream metadata.

        Resolution is only a fallback. The old "height == 720 means BT.709"
        heuristic incorrectly classified 720x480/576 SD content, so the
        fallback follows the SD/HD raster boundary instead.
        """
        width = self._positive_int(width, "width")
        height = self._positive_int(height, "height")

        if metadata:
            if not isinstance(metadata, Mapping):
                raise ValueError("metadata must be a mapping")
            for key in ("matrix", "matrix_coefficients", "matrix_coefficients_id",
                        "color_matrix", "colour_matrix"):
                if key not in metadata or metadata[key] is None:
                    continue
                value = metadata[key]
                if isinstance(value, str) and value.strip().lower() in {
                    "", "unspecified", "unknown", "auto", "default"
                }:
                    break
                if isinstance(value, int) and value in (0, 2):
                    break
                return self._normalize_matrix(value)

        # BT.601 remains the sensible fallback for SD rasters. Anything
        # beyond the SD envelope is treated as HD/wide-gamut content.
        if width <= 1024 and height <= 576:
            return "bt601"
        return "bt709"

    def range_scale(self, value, src=None, dst=None, bits=8):
        if isinstance(bits, bool) or not isinstance(bits, int) or bits < 8:
            raise ValueError("bits must be an integer >= 8")
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("value must be a finite number")

        src_name = self._normalize_range(self.src_range if src is None else src)
        dst_name = self._normalize_range(self.dst_range if dst is None else dst)
        maximum = (1 << bits) - 1
        scale = 1 << (bits - 8)
        limited_black = 16 * scale
        limited_span = 219 * scale

        if src_name == dst_name:
            scaled = float(value)
        elif src_name == "limited":
            scaled = (float(value) - limited_black) * maximum / limited_span
        else:
            scaled = limited_black + float(value) * limited_span / maximum

        return max(0, min(maximum, self._round_half_up(scaled)))

    @staticmethod
    def _extract_rgb(rgb):
        if isinstance(rgb, Mapping):
            try:
                channels = (rgb["r"], rgb["g"], rgb["b"])
            except KeyError as exc:
                raise ValueError("RGB mapping requires r, g and b channels") from exc
        elif isinstance(rgb, Sequence) and not isinstance(rgb, (str, bytes)):
            if len(rgb) < 3:
                raise ValueError("RGB input requires three channels")
            channels = rgb[:3]
        else:
            raise ValueError("RGB input must be a sequence or mapping")

        result = []
        for channel in channels:
            if not isinstance(channel, (int, float)) or not math.isfinite(channel):
                raise ValueError("RGB channels must be finite numbers")
            result.append(max(0.0, min(255.0, float(channel))))
        return tuple(result)

    def _rgb_to_yuv_unclipped(self, rgb, matrix=None):
        red, green, blue = self._extract_rgb(rgb)
        coeff = self.coefficients(matrix)
        kr, kg, kb = coeff["kr"], coeff["kg"], coeff["kb"]
        y = kr * red + kg * green + kb * blue
        u = 128.0 + 0.5 * (blue - y) / (1.0 - kb)
        v = 128.0 + 0.5 * (red - y) / (1.0 - kr)
        return (y, u, v)

    def rgb_to_yuv(self, rgb, matrix=None):
        y, u, v = self._rgb_to_yuv_unclipped(rgb, matrix)
        return {
            "y": max(0.0, min(255.0, y)),
            "u": max(0.0, min(255.0, u)),
            "v": max(0.0, min(255.0, v)),
        }

    @staticmethod
    def _yuv_to_rgb_unclipped(yuv, coeff):
        y, u, v = yuv
        kr, kg, kb = coeff["kr"], coeff["kg"], coeff["kb"]
        u_centered = u - 128.0
        v_centered = v - 128.0
        red = y + 2.0 * (1.0 - kr) * v_centered
        blue = y + 2.0 * (1.0 - kb) * u_centered
        green = (y - kr * red - kb * blue) / kg
        return red, green, blue

    def chroma_size(self, width, height):
        width = self._positive_int(width, "width")
        height = self._positive_int(height, "height")
        return (width + 1) // 2, (height + 1) // 2

    def plane_layout(self, fmt, width, height):
        width = self._positive_int(width, "width")
        height = self._positive_int(height, "height")
        token = self._normalize_token(fmt)
        aliases = {
            "i420": "i420",
            "i420p": "i420",
            "yuv420p": "i420",
            "yv12": "yv12",
            "nv12": "nv12",
            "nv21": "nv21",
        }
        try:
            normalized = aliases[token]
        except KeyError as exc:
            raise ValueError(f"unsupported pixel format: {fmt!r}") from exc

        chroma_width, chroma_height = self.chroma_size(width, height)
        chroma_samples = chroma_width * chroma_height
        luma_size = width * height
        interleaved = normalized in {"nv12", "nv21"}

        return {
            "format": normalized,
            "width": width,
            "height": height,
            "y": luma_size,
            "u": 0 if interleaved else chroma_samples,
            "v": 0 if interleaved else chroma_samples,
            "uv": chroma_samples * 2,
            "interleaved": interleaved,
        }

    def chroma_offset(self, siting):
        token = self._normalize_token(siting)
        try:
            return self._CHROMA_SITING[token]
        except KeyError as exc:
            raise ValueError(f"unknown chroma siting: {siting!r}") from exc

    @staticmethod
    def _pq_eotf(pq_value: float) -> float:
        """Return display luminance in cd/m² for a normalized PQ signal."""
        m1 = 2610.0 / 16384.0
        m2 = 2523.0 / 4096.0 * 128.0
        c1 = 3424.0 / 4096.0
        c2 = 2413.0 / 4096.0 * 32.0
        c3 = 2392.0 / 4096.0 * 32.0
        p = max(0.0, min(1.0, float(pq_value))) ** (1.0 / m2)
        numerator = max(p - c1, 0.0)
        denominator = c2 - c3 * p
        if denominator <= 0.0:
            return 0.0
        return 10000.0 * (numerator / denominator) ** (1.0 / m1)

    def tone_map(self, pq_value, peak_nits=1000.0):
        """Map PQ to SDR with a soft shoulder instead of a hard linear clip.

        The shoulder keeps values above the SDR diffuse-white point ordered
        and distinguishable all the way to the HDR peak.
        """
        if isinstance(peak_nits, bool) or not isinstance(peak_nits, (int, float)):
            raise ValueError("peak_nits must be a positive number")
        if not math.isfinite(peak_nits) or peak_nits <= 0:
            raise ValueError("peak_nits must be a positive number")

        luminance = self._pq_eotf(pq_value)
        if luminance <= 0.0:
            return 0.0

        # A Reinhard shoulder anchored at SDR diffuse white. It is monotonic,
        # maps 100 cd/m² to 0.5, and still gives 10,000 cd/m² a value below 1.
        shoulder = min(100.0, float(peak_nits))
        mapped = luminance / (luminance + shoulder)
        return max(0.0, min(1.0, mapped))

    def _channel_dict(self, color, keys=("r", "g", "b")):
        if isinstance(color, Mapping):
            try:
                values = {key: float(color[key]) for key in keys}
            except KeyError as exc:
                raise ValueError("color mapping is missing a channel") from exc
            alpha = color.get("a", 1.0)
            if "premultiplied" in color:
                premultiplied = bool(color["premultiplied"])
            else:
                premultiplied = self.alpha_mode == "premultiplied"
            return values, float(alpha), premultiplied
        if isinstance(color, Sequence) and not isinstance(color, (str, bytes)):
            if len(color) < 4:
                raise ValueError("color sequence requires r, g, b and a")
            values = {key: float(value) for key, value in zip(keys, color[:3])}
            return values, float(color[3]), self.alpha_mode == "premultiplied"
        raise ValueError("color must be a sequence or mapping")

    def alpha_blend(self, color, background=(0, 0, 0)):
        values, alpha, premultiplied = self._channel_dict(color)
        if not math.isfinite(alpha):
            raise ValueError("alpha must be finite")
        if alpha > 1.0:
            alpha /= 255.0
        alpha = max(0.0, min(1.0, alpha))

        if isinstance(background, Mapping):
            background_values = {key: float(background[key]) for key in ("r", "g", "b")}
        else:
            if len(background) < 3:
                raise ValueError("background requires three channels")
            background_values = {
                key: float(value) for key, value in zip(("r", "g", "b"), background[:3])
            }

        result = {}
        for key in ("r", "g", "b"):
            source = values[key]
            if not premultiplied:
                source *= alpha
            blended = source + background_values[key] * (1.0 - alpha)
            result[key] = max(0.0, min(255.0, blended))
        result["a"] = alpha
        return result

    def convert_chain(self, rgb, steps=4):
        """Simulate repeated RGB→YUV→RGB passes with one final quantization."""
        if isinstance(steps, bool) or not isinstance(steps, int) or steps <= 0:
            raise ValueError("steps must be a positive integer")

        current = self._extract_rgb(rgb)
        coeff = self.coefficients(self.src_matrix)
        for _ in range(steps):
            # Deliberately do not clip or round between passes. Quantizing
            # every leg is what creates banding after a few generations.
            current = self._yuv_to_rgb_unclipped(
                self._rgb_to_yuv_unclipped(current, self.src_matrix), coeff
            )
        return tuple(self._round_half_up(max(0.0, min(255.0, value))) for value in current)

    def tile_ranges(self, width, height, tile_size):
        width = self._positive_int(width, "width")
        height = self._positive_int(height, "height")
        tile_size = self._positive_int(tile_size, "tile_size")

        ranges = []
        for y0 in range(0, height, tile_size):
            y1 = min(height, y0 + tile_size)
            for x0 in range(0, width, tile_size):
                x1 = min(width, x0 + tile_size)
                ranges.append((x0, x1, y0, y1))
        return ranges

    def process_chunks(self, width, lanes=4):
        width = self._positive_int(width, "width")
        lanes = self._positive_int(lanes, "lanes")
        return [
            (start, min(width, start + lanes))
            for start in range(0, width, lanes)
        ]
