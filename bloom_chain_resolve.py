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


class BloomChainResolve:
    def __init__(self, threshold=1.0, intensity=1.0, exposure=1.0):
        self.threshold = threshold
        self.intensity = intensity
        self.exposure = exposure

    def srgb_to_linear(self, value):
        raise NotImplementedError

    def extract(self, pixels, width, height, threshold=None, exposure=None):
        raise NotImplementedError

    def downsample(self, image, width, height):
        raise NotImplementedError

    def upsample_add(self, base, small, small_width, small_height):
        raise NotImplementedError

    def build_chain(self, width, height):
        raise NotImplementedError

    def resolve(self, pixels, width, height, target="main", intensity=None,
                threshold=None, exposure=None):
        raise NotImplementedError

    def allocations(self, target="main"):
        raise NotImplementedError

    def history_size(self, target="main"):
        raise NotImplementedError

    def camera_cut(self):
        raise NotImplementedError
