"""Color matrix, range and chroma conversion helpers."""


class ColorMatrixConverter:
    def __init__(self, src_matrix="bt709", src_range="limited", dst_matrix="bt709",
                 dst_range="full", alpha_mode="straight"):
        self.src_matrix = src_matrix
        self.src_range = src_range
        self.dst_matrix = dst_matrix
        self.dst_range = dst_range
        self.alpha_mode = alpha_mode

    def coefficients(self, matrix=None):
        raise NotImplementedError

    def infer_matrix(self, width, height, metadata=None):
        raise NotImplementedError

    def range_scale(self, value, src=None, dst=None, bits=8):
        raise NotImplementedError

    def rgb_to_yuv(self, rgb, matrix=None):
        raise NotImplementedError

    def chroma_size(self, width, height):
        raise NotImplementedError

    def plane_layout(self, fmt, width, height):
        raise NotImplementedError

    def chroma_offset(self, siting):
        raise NotImplementedError

    def tone_map(self, pq_value, peak_nits=1000.0):
        raise NotImplementedError

    def alpha_blend(self, color, background=(0, 0, 0)):
        raise NotImplementedError

    def convert_chain(self, rgb, steps=4):
        raise NotImplementedError

    def tile_ranges(self, width, height, tile_size):
        raise NotImplementedError

    def process_chunks(self, width, lanes=4):
        raise NotImplementedError
