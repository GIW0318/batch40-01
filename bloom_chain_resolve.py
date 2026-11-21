"""HDR bloom chain.

Contract used by the tests
--------------------------
* ``srgb_to_linear(v)``: standard sRGB transfer (v <= 0.04045 -> v/12.92,
  else ((v+0.055)/1.055) ** 2.4).
* ``extract(pixels, width, height, threshold=None, exposure=None)``:
  converts each sRGB pixel to linear, multiplies by exposure, subtracts the
  threshold and clamps at 0.  NaN and infinity become 0.  Returns a list of
  the same length.
* ``downsample(image, width, height)`` returns (out, out_w, out_h) with
  out_w=(width+1)//2 and out_h=(height+1)//2; every output cell is the mean
  of its up-to-2x2 source cells, so odd trailing rows/columns are not
  dropped.
* ``upsample_add(base, small, small_width, small_height)`` returns base plus
  the nearest-neighbour upsampled small image (output index i uses
  small[i * small_width // len(base)]); base is added exactly once.
* ``build_chain(width, height)`` returns [(w,h), ...] halving with ceil
  until 1x1.
* ``resolve(pixels, width, height, target="main", intensity=None,
  threshold=None, exposure=None)`` returns {"target", "sizes", "output",
  "skipped"}.  intensity 0 skips the chain: sizes == [], output is a copy of
  the input and skipped is True.  Otherwise it extracts bright pixels,
  builds the mip chain for the target dimensions (rebuilding when the size
  changed), accumulates the upsampled levels, scales by intensity and adds
  the result to the input.  Render targets are reused across frames for the
  same target and size; ``allocations(target="main")`` counts targets
  created so far and must not grow on a repeated resolve with unchanged
  size.  A skipped resolve allocates nothing.
* ``resolve`` appends one entry to the temporal history of the target.
  ``camera_cut()`` clears all history and returns the number of entries
  removed; ``history_size(target="main")`` returns the current count.
* Each target has its own render targets, so resolving "ui" does not touch
  "main" output.
"""

import math


class BloomChainResolve:
    def __init__(self, threshold=1.0, intensity=1.0, exposure=1.0):
        self.threshold = threshold
        self.intensity = intensity
        self.exposure = exposure
        self._targets = {}
        self._allocations = {}
        self._history = {}

    @staticmethod
    def _finite(value, fallback=0.0):
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            return fallback
        return value if math.isfinite(value) else fallback

    def srgb_to_linear(self, value):
        value = self._finite(value)
        if value <= 0.04045:
            return value / 12.92
        try:
            return ((value + 0.055) / 1.055) ** 2.4
        except OverflowError:
            return 0.0

    def extract(self, pixels, width, height, threshold=None, exposure=None):
        threshold = self._finite(
            self.threshold if threshold is None else threshold
        )
        exposure = self._finite(
            self.exposure if exposure is None else exposure
        )
        result = []
        for pixel in pixels:
            linear = self.srgb_to_linear(pixel) * exposure
            value = linear - threshold
            if not math.isfinite(value) or value < 0.0:
                value = 0.0
            result.append(value)
        return result

    def downsample(self, image, width, height):
        width = int(width)
        height = int(height)
        if width <= 0 or height <= 0:
            return [], 0, 0

        out_width = (width + 1) // 2
        out_height = (height + 1) // 2
        result = []
        for out_y in range(out_height):
            y0 = out_y * 2
            y1 = min(y0 + 2, height)
            for out_x in range(out_width):
                x0 = out_x * 2
                x1 = min(x0 + 2, width)
                total = 0.0
                count = 0
                for y in range(y0, y1):
                    row = y * width
                    for x in range(x0, x1):
                        index = row + x
                        if index < len(image):
                            total += self._finite(image[index])
                            count += 1
                result.append(total / count if count else 0.0)
        return result, out_width, out_height

    @staticmethod
    def _infer_base_shape(count, small_width, small_height):
        if count <= 0:
            return 0, 0

        candidates = []
        for base_width in (max(1, small_width * 2 - 1),
                           max(1, small_width * 2)):
            for base_height in (max(1, small_height * 2 - 1),
                                max(1, small_height * 2)):
                if base_width * base_height == count:
                    candidate = (base_width, base_height)
                    if candidate not in candidates:
                        candidates.append(candidate)

        if not candidates:
            for base_width in range(max(1, small_width), count + 1):
                if count % base_width:
                    continue
                base_height = count // base_width
                if ((base_width + 1) // 2 == small_width and
                        (base_height + 1) // 2 == small_height):
                    candidates.append((base_width, base_height))

        if candidates:
            candidates.sort(
                key=lambda shape: (
                    abs(shape[0] - small_width * 2) +
                    abs(shape[1] - small_height * 2),
                    shape[1],
                    shape[0],
                )
            )
            return candidates[0]

        base_width = min(max(1, small_width), count)
        while base_width > 1 and count % base_width:
            base_width -= 1
        return base_width, max(1, count // base_width)

    def _upsample_add_2d(self, base, base_width, base_height, small,
                         small_width, small_height):
        result = []
        small_width = max(1, int(small_width))
        small_height = max(1, int(small_height))
        base_width = max(1, int(base_width))
        base_height = max(1, int(base_height))

        for y in range(base_height):
            small_y = min(small_height - 1, y * small_height // base_height)
            small_row = small_y * small_width
            base_row = y * base_width
            for x in range(base_width):
                small_x = min(small_width - 1, x * small_width // base_width)
                base_index = base_row + x
                small_index = small_row + small_x
                base_value = (self._finite(base[base_index])
                              if base_index < len(base) else 0.0)
                small_value = (self._finite(small[small_index])
                               if small_index < len(small) else 0.0)
                value = base_value + small_value
                result.append(value if math.isfinite(value) else 0.0)
        return result

    def upsample_add(self, base, small, small_width, small_height):
        if not base:
            return []
        base_width, base_height = self._infer_base_shape(
            len(base), small_width, small_height
        )
        return self._upsample_add_2d(
            base, base_width, base_height,
            small, small_width, small_height,
        )

    def build_chain(self, width, height):
        width = int(width)
        height = int(height)
        if width <= 0 or height <= 0:
            return []

        sizes = [(width, height)]
        while width > 1 or height > 1:
            width = (width + 1) // 2
            height = (height + 1) // 2
            sizes.append((width, height))
        return sizes

    def resolve(self, pixels, width, height, target="main", intensity=None,
                threshold=None, exposure=None):
        intensity = self._finite(
            self.intensity if intensity is None else intensity
        )
        threshold = self._finite(
            self.threshold if threshold is None else threshold
        )
        exposure = self._finite(
            self.exposure if exposure is None else exposure
        )
        width = int(width)
        height = int(height)
        history = self._history.setdefault(target, [])

        if intensity == 0.0:
            history.append({"width": width, "height": height, "skipped": True})
            return {
                "target": target,
                "sizes": [],
                "output": list(pixels),
                "skipped": True,
            }

        sizes = self.build_chain(width, height)
        state = self._targets.get(target)
        if (state is None or state["width"] != width or
                state["height"] != height):
            state = {
                "width": width,
                "height": height,
                "sizes": sizes,
                "levels": [[0.0] * (level_width * level_height)
                           for level_width, level_height in sizes],
            }
            self._targets[target] = state
            self._allocations[target] = (
                self._allocations.get(target, 0) + len(sizes)
            )

        source = [self._finite(value) for value in pixels]
        levels = state["levels"]
        levels[0][:] = self.extract(
            source, width, height, threshold=threshold, exposure=exposure
        )[:len(levels[0])]

        for index in range(1, len(sizes)):
            source_width, source_height = sizes[index - 1]
            downsampled, _, _ = self.downsample(
                levels[index - 1], source_width, source_height
            )
            levels[index][:] = downsampled[:len(levels[index])]

        accumulated = list(levels[-1])
        for index in range(len(sizes) - 2, -1, -1):
            base_width, base_height = sizes[index]
            small_width, small_height = sizes[index + 1]
            accumulated = self._upsample_add_2d(
                levels[index], base_width, base_height,
                accumulated, small_width, small_height,
            )

        result = []
        for index, value in enumerate(source):
            bloom = (
                accumulated[index] * intensity
                if index < len(accumulated) else 0.0
            )
            output = value + bloom
            result.append(output if math.isfinite(output) else 0.0)

        history.append({"width": width, "height": height, "skipped": False})
        return {
            "target": target,
            "sizes": list(sizes),
            "output": result,
            "skipped": False,
        }

    def allocations(self, target="main"):
        return self._allocations.get(target, 0)

    def history_size(self, target="main"):
        return len(self._history.get(target, ()))

    def camera_cut(self):
        removed = sum(len(entries) for entries in self._history.values())
        self._history.clear()
        return removed
