"""Tiled navmesh baking: seam stitching, links and incremental updates."""

import math
import threading


class NavMeshTileBaker:
    def __init__(self, tile_size=32.0, cell_size=0.5, agent_radius=0.6,
                 step_height=0.4, max_slope=45.0):
        if tile_size <= 0:
            raise ValueError("tile_size must be positive")
        if cell_size <= 0:
            raise ValueError("cell_size must be positive")
        if agent_radius <= 0:
            raise ValueError("agent_radius must be positive")
        if step_height < 0:
            raise ValueError("step_height must be non-negative")
        if not 0.0 < max_slope <= 90.0:
            raise ValueError("max_slope must be within (0, 90] degrees")
        self.tile_size = tile_size
        self.cell_size = cell_size
        self.agent_radius = agent_radius
        self.step_height = step_height
        self.max_slope = max_slope
        self.tiles = {}
        self.offmesh = []
        self.obstacles = {}
        self._baked_version = {}
        self._lock = threading.RLock()

    def add_tile(self, tx, ty, edges, version="v1"):
        with self._lock:
            key = (tx, ty)
            if key in self.tiles:
                raise ValueError("tile %r already registered" % (key,))
            self.tiles[key] = {
                "coord": key,
                "edges": list(edges),
                "version": version,
            }
            self.obstacles.setdefault(key, set())
            return self.tiles[key]

    def stitch(self, a, b):
        with self._lock:
            tile_a = self.tiles.get(a)
            tile_b = self.tiles.get(b)
            if tile_a is None or tile_b is None:
                return self._portal(a, b, False, "missing_tile", 0.0)

            side_b = self._shared_side(a, b)
            if side_b is None:
                return self._portal(a, b, False, "not_adjacent", 0.0)
            side_a = self._opposite_side(side_b)

            edge_a = self._edge_on_side(tile_a, side_a)
            edge_b = self._edge_on_side(tile_b, side_b)
            if edge_a is None or edge_b is None:
                return self._portal(a, b, False, "no_edge", 0.0)

            height_gap = abs(edge_a["start"][2] - edge_b["start"][2])
            if height_gap > self.step_height:
                return self._portal(a, b, False, "height_gap", 0.0)

            for edge in (edge_a, edge_b):
                if edge["slope_deg"] > self.max_slope:
                    return self._portal(a, b, False, "slope", 0.0)

            width = self._edge_length(edge_a) - 2.0 * self.agent_radius
            if width <= 0.0:
                return self._portal(a, b, False, "too_narrow", 0.0)

            return self._portal(a, b, True, None, width)

    def bake(self, tiles=None):
        with self._lock:
            if tiles is None:
                targets = sorted(self.tiles)
            else:
                targets = sorted(tuple(coord) for coord in tiles)

            baked = []
            reused = 0
            for key in targets:
                if key not in self.tiles:
                    continue
                if self._baked_version.get(key) == self.tiles[key]["version"]:
                    reused += 1
                else:
                    self._baked_version[key] = self.tiles[key]["version"]
                    baked.append(key)

            portals = []
            seen = set()
            for key in sorted(self.tiles):
                for neighbour in self._neighbour_coords(key):
                    if neighbour not in self.tiles:
                        continue
                    pair_key = tuple(sorted((key, neighbour)))
                    if pair_key in seen:
                        continue
                    seen.add(pair_key)
                    portal = self.stitch(pair_key[0], pair_key[1])
                    if portal["connected"]:
                        portals.append(portal)

            return {
                "baked_tiles": len(baked),
                "reused": reused,
                "portals": portals,
            }

    def offmesh_link(self, a, b, bidir=True):
        with self._lock:
            a = tuple(a)
            b = tuple(b)
            link = {"from": a, "to": b, "bidir": bool(bidir)}
            if link not in self.offmesh:
                self.offmesh.append(link)
            if bidir:
                reverse = {"from": b, "to": a, "bidir": True}
                if reverse not in self.offmesh:
                    self.offmesh.append(reverse)

    def links(self):
        with self._lock:
            return [dict(link) for link in self.offmesh]

    def apply_obstacle(self, obstacle):
        with self._lock:
            min_x, min_y, max_x, max_y = obstacle["bounds"]
            obstacle_id = obstacle["id"]
            min_gx = int(math.floor(min_x / self.cell_size))
            min_gy = int(math.floor(min_y / self.cell_size))
            max_gx = int(math.ceil(max_x / self.cell_size))
            max_gy = int(math.ceil(max_y / self.cell_size))
            cells_per_tile = int(round(self.tile_size / self.cell_size))

            for gx in range(min_gx, max_gx):
                for gy in range(min_gy, max_gy):
                    tx = self._floor_div(gx, cells_per_tile)
                    ty = self._floor_div(gy, cells_per_tile)
                    if (tx, ty) not in self.tiles:
                        continue
                    local = (gx - tx * cells_per_tile,
                             gy - ty * cells_per_tile)
                    self.obstacles.setdefault((tx, ty), set()).add(
                        (obstacle_id, local[0], local[1]))

    def carved_cells(self, tx, ty):
        with self._lock:
            return sorted(self.obstacles.get((tx, ty), set()))

    def query(self, point):
        with self._lock:
            cells_per_tile = int(round(self.tile_size / self.cell_size))
            gx = int(math.floor(point["x"] / self.tile_size))
            gy = int(math.floor(point["y"] / self.tile_size))

            on_x_seam = self._is_seam(point["x"])
            on_y_seam = self._is_seam(point["y"])
            candidates = {(gx, gy)}
            if on_x_seam:
                candidates.add((gx - 1, gy))
            if on_y_seam:
                candidates.add((gx, gy - 1))
            if on_x_seam and on_y_seam:
                candidates.add((gx - 1, gy - 1))

            available = [coord for coord in candidates if coord in self.tiles]
            if available:
                tile = min(available)
            else:
                tile = (gx, gy)
            return {"tile": tile,
                    "cell": (
                        int(math.floor(point["x"] / self.cell_size))
                        - tile[0] * cells_per_tile,
                        int(math.floor(point["y"] / self.cell_size))
                        - tile[1] * cells_per_tile)}

    def index_width(self, vertex_count):
        if vertex_count <= 0xFFFF:
            return 16
        if vertex_count <= 0xFFFFFFFF:
            return 32
        return 64

    def set_tile_version(self, tx, ty, version):
        with self._lock:
            key = (tx, ty)
            if key not in self.tiles:
                raise KeyError("unknown tile %r" % (key,))
            self.tiles[key]["version"] = version

    def dirty_tiles(self):
        with self._lock:
            return sorted(
                key for key, tile in self.tiles.items()
                if self._baked_version.get(key) != tile["version"])

    @staticmethod
    def _opposite_side(side):
        return {"east": "west", "west": "east",
                "north": "south", "south": "north"}[side]

    @staticmethod
    def _shared_side(a, b):
        dx = b[0] - a[0]
        dy = b[1] - a[1]
        if dx == 1 and dy == 0:
            return "west"
        if dx == -1 and dy == 0:
            return "east"
        if dx == 0 and dy == 1:
            return "south"
        if dx == 0 and dy == -1:
            return "north"
        return None

    @staticmethod
    def _edge_on_side(tile, side):
        for edge in tile["edges"]:
            if edge["side"] == side:
                return edge
        return None

    @staticmethod
    def _edge_length(edge):
        start = edge["start"]
        end = edge["end"]
        return math.sqrt(sum((end[i] - start[i]) ** 2 for i in range(3)))

    @staticmethod
    def _neighbour_coords(key):
        tx, ty = key
        return [(tx + 1, ty), (tx - 1, ty),
                (tx, ty + 1), (tx, ty - 1)]

    @staticmethod
    def _floor_div(value, divisor):
        return value // divisor

    def _is_seam(self, value):
        ratio = value / self.tile_size
        return abs(ratio - round(ratio)) < 1e-9

    @staticmethod
    def _portal(a, b, connected, reason, width):
        return {
            "a": tuple(a),
            "b": tuple(b),
            "connected": connected,
            "reason": reason,
            "width": width,
        }
