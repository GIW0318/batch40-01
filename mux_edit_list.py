"""MP4 mux sample tables, edit lists and ctts handling."""


class MuxEditList:
    def __init__(self, timescale=90000, max_chunk=65536):
        self.timescale = timescale
        self.max_chunk = max_chunk
        self.tracks = {}

    def add_track(self, name, samples, timescale=None, priming=0):
        raise NotImplementedError

    def plan(self):
        raise NotImplementedError

    def edit_list(self, name):
        raise NotImplementedError

    def ctts(self, name):
        raise NotImplementedError

    def interleave(self, max_chunk=None):
        raise NotImplementedError

    def metadata_order(self):
        raise NotImplementedError

    def to_ticks(self, seconds):
        raise NotImplementedError

    def build_moov(self, order=None):
        raise NotImplementedError

    def duration(self):
        raise NotImplementedError
