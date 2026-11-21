"""Tiled navmesh baking: seam stitching, links and incremental updates."""


class NavMeshTileBaker:
    def __init__(self, tile_size=32.0, cell_size=0.5, agent_radius=0.6,
                 step_height=0.4, max_slope=45.0):
        self.tile_size = tile_size
        self.cell_size = cell_size
        self.agent_radius = agent_radius
        self.step_height = step_height
        self.max_slope = max_slope
        self.tiles = {}
        self.offmesh = []
        self.obstacles = {}

    def add_tile(self, tx, ty, edges, version="v1"):
        raise NotImplementedError

    def stitch(self, a, b):
        raise NotImplementedError

    def bake(self, tiles=None):
        raise NotImplementedError

    def offmesh_link(self, a, b, bidir=True):
        raise NotImplementedError

    def links(self):
        raise NotImplementedError

    def apply_obstacle(self, obstacle):
        raise NotImplementedError

    def carved_cells(self, tx, ty):
        raise NotImplementedError

    def query(self, point):
        raise NotImplementedError

    def index_width(self, vertex_count):
        raise NotImplementedError

    def set_tile_version(self, tx, ty, version):
        raise NotImplementedError

    def dirty_tiles(self):
        raise NotImplementedError
