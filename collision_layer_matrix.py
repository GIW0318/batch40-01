"""Wide collision layer masks, symmetry and compound body inheritance."""

import threading


class CollisionLayerMatrix:
    def __init__(self, layer_count=32, max_layers=256):
        if not isinstance(layer_count, int) or isinstance(layer_count, bool):
            raise ValueError("layer_count must be an integer")
        if not isinstance(max_layers, int) or isinstance(max_layers, bool):
            raise ValueError("max_layers must be an integer")
        if layer_count < 1:
            raise ValueError("layer_count must be at least 1")
        if max_layers < 1:
            raise ValueError("max_layers must be at least 1")
        if layer_count > max_layers:
            raise ValueError(
                f"layer_count {layer_count} exceeds max_layers {max_layers}"
            )
        self.layer_count = layer_count
        self.max_layers = max_layers
        self.names = {}
        self._name_to_id = {}
        self.collision = set()
        self.trigger = set()
        self.bodies = {}
        self.children = {}
        self.broadphase = []
        self.dirty = True
        self._lock = threading.RLock()

    def _check_layer(self, layer):
        if not isinstance(layer, int) or isinstance(layer, bool):
            raise ValueError(f"layer index must be an integer, got {layer!r}")
        if layer < 0 or layer >= self.layer_count:
            raise ValueError(
                f"layer index {layer} out of range [0, {self.layer_count})"
            )

    def _check_mask(self, mask):
        if not isinstance(mask, int) or isinstance(mask, bool) or mask < 0:
            raise ValueError("query mask must be a non-negative integer")

    @staticmethod
    def _pair(a, b):
        return (a, b) if a <= b else (b, a)

    def _find_body_owner(self, body_id):
        if body_id in self.bodies:
            return None
        for parent_id, children in self.children.items():
            if body_id in children:
                return parent_id
        return None

    def define_layer(self, name, layer_id=None):
        with self._lock:
            if not isinstance(name, str) or not name:
                raise ValueError("layer name must be a non-empty string")
            if name in self._name_to_id:
                raise ValueError(f"layer name {name!r} is already in use")
            if layer_id is None:
                layer_id = next(
                    (idx for idx in range(self.layer_count)
                     if idx not in self.names),
                    None,
                )
                if layer_id is None:
                    raise ValueError("no free layer slot available")
            self._check_layer(layer_id)
            if layer_id in self.names:
                raise ValueError(f"layer {layer_id} is already defined")
            self.names[layer_id] = name
            self._name_to_id[name] = layer_id
            return layer_id

    def rename_layer(self, layer_id, new_name):
        with self._lock:
            self._check_layer(layer_id)
            if not isinstance(new_name, str) or not new_name:
                raise ValueError("layer name must be a non-empty string")
            owner = self._name_to_id.get(new_name)
            if owner is not None and owner != layer_id:
                raise ValueError(f"layer name {new_name!r} is already in use")
            previous = self.names.get(layer_id)
            if previous is not None:
                del self._name_to_id[previous]
            self.names[layer_id] = new_name
            self._name_to_id[new_name] = layer_id

    def layer_id(self, name):
        with self._lock:
            try:
                return self._name_to_id[name]
            except KeyError:
                raise KeyError(f"unknown layer name {name!r}") from None

    def name_of(self, layer_id):
        with self._lock:
            self._check_layer(layer_id)
            return self.names.get(layer_id)

    def set_collision(self, a, b, enabled=True):
        with self._lock:
            self._check_layer(a)
            self._check_layer(b)
            pair = self._pair(a, b)
            if enabled:
                self.collision.add(pair)
            else:
                self.collision.discard(pair)
            self.dirty = True

    def collides(self, a, b):
        with self._lock:
            self._check_layer(a)
            self._check_layer(b)
            return self._pair(a, b) in self.collision

    def set_trigger(self, a, b, enabled=True):
        with self._lock:
            self._check_layer(a)
            self._check_layer(b)
            pair = self._pair(a, b)
            if enabled:
                self.trigger.add(pair)
            else:
                self.trigger.discard(pair)
            self.dirty = True

    def trigger_collides(self, a, b):
        with self._lock:
            self._check_layer(a)
            self._check_layer(b)
            return self._pair(a, b) in self.trigger

    def mask_for(self, layer):
        with self._lock:
            self._check_layer(layer)
            return 1 << layer

    def add_body(self, body_id, layer, enabled=True, trigger=False):
        with self._lock:
            self._check_layer(layer)
            if body_id in self.bodies or self._find_body_owner(body_id) is not None:
                raise ValueError(f"body {body_id!r} already exists")
            self.bodies[body_id] = {
                "layer": layer,
                "enabled": bool(enabled),
                "trigger": bool(trigger),
            }
            self.dirty = True

    def add_child(self, parent_id, child_id, layer=None):
        with self._lock:
            if parent_id not in self.bodies:
                raise ValueError(f"unknown parent body {parent_id!r}")
            if child_id in self.bodies or self._find_body_owner(child_id) is not None:
                raise ValueError(f"body {child_id!r} already exists")
            if layer is not None:
                self._check_layer(layer)
            self.children.setdefault(parent_id, {})[child_id] = layer
            self.dirty = True

    def _child_layer(self, parent_id, explicit_layer):
        if explicit_layer is not None:
            return explicit_layer
        return self.bodies[parent_id]["layer"]

    def set_body_layer(self, body_id, layer):
        with self._lock:
            self._check_layer(layer)
            if body_id in self.bodies:
                self.bodies[body_id]["layer"] = layer
                self.dirty = True
                return
            for children in self.children.values():
                if body_id in children:
                    children[body_id] = layer
                    self.dirty = True
                    return
            raise ValueError(f"unknown body {body_id!r}")

    def disable_body(self, body_id):
        with self._lock:
            record = self.bodies.get(body_id)
            if record is None:
                raise ValueError(f"unknown body {body_id!r}")
            record["enabled"] = False
            self.dirty = True

    def query_bodies(self, query_mask, trigger=False):
        with self._lock:
            self._check_mask(query_mask)
            result = []
            for parent_id, children in self.children.items():
                parent = self.bodies.get(parent_id)
                if parent is None or not parent["enabled"]:
                    continue
                if parent["trigger"] != trigger:
                    continue
                for child_id, explicit_layer in children.items():
                    layer = self._child_layer(parent_id, explicit_layer)
                    if (1 << layer) & query_mask:
                        result.append(child_id)
            for body_id, record in self.bodies.items():
                if not record["enabled"] or record["trigger"] != trigger:
                    continue
                if (1 << record["layer"]) & query_mask:
                    result.append(body_id)
            return result

    def pairs(self):
        with self._lock:
            return list(self.broadphase)

    def rebuild(self):
        with self._lock:
            active = [
                (body_id, record)
                for body_id, record in self.bodies.items()
                if record["enabled"]
            ]
            pairs = []
            for index in range(len(active)):
                body_a, record_a = active[index]
                for other in range(index + 1, len(active)):
                    body_b, record_b = active[other]
                    if self._pair(record_a["layer"], record_b["layer"]) in self.collision:
                        pairs.append(
                            (body_a, body_b) if body_a <= body_b else (body_b, body_a)
                        )
            pairs.sort()
            self.broadphase = pairs
            self.dirty = False

    def serialize(self):
        with self._lock:
            return {
                "version": 1,
                "layer_count": self.layer_count,
                "max_layers": self.max_layers,
                "layers": [
                    {"id": layer_id, "name": name}
                    for layer_id, name in sorted(self.names.items())
                ],
                "collision": [list(pair) for pair in sorted(self.collision)],
                "trigger": [list(pair) for pair in sorted(self.trigger)],
                "bodies": [
                    {
                        "id": body_id,
                        "layer": record["layer"],
                        "enabled": record["enabled"],
                        "trigger": record["trigger"],
                    }
                    for body_id, record in self.bodies.items()
                ],
                "children": [
                    {
                        "parent": parent_id,
                        "child": child_id,
                        "layer": explicit_layer,
                    }
                    for parent_id, children in self.children.items()
                    for child_id, explicit_layer in children.items()
                ],
            }

    def load(self, data):
        with self._lock:
            if not isinstance(data, dict):
                raise ValueError("serialized data must be a mapping")
            try:
                layer_count = data["layer_count"]
                max_layers = data["max_layers"]
            except KeyError as missing:
                raise ValueError(f"missing serialized field {missing.args[0]!r}") from None
            if not isinstance(layer_count, int) or isinstance(layer_count, bool):
                raise ValueError("layer_count must be an integer")
            if not isinstance(max_layers, int) or isinstance(max_layers, bool):
                raise ValueError("max_layers must be an integer")
            if layer_count < 1 or max_layers < 1 or layer_count > max_layers:
                raise ValueError("invalid layer count limits in serialized data")

            self.layer_count = layer_count
            self.max_layers = max_layers
            self.names = {}
            self._name_to_id = {}
            self.collision = set()
            self.trigger = set()
            self.bodies = {}
            self.children = {}
            self.broadphase = []
            self.dirty = True

            for entry in data.get("layers", []):
                layer_id = entry["id"]
                name = entry["name"]
                self._check_layer(layer_id)
                if not isinstance(name, str) or not name:
                    raise ValueError("layer name must be a non-empty string")
                if name in self._name_to_id or layer_id in self.names:
                    raise ValueError("duplicate layer in serialized data")
                self.names[layer_id] = name
                self._name_to_id[name] = layer_id

            for key, target in (("collision", self.collision),
                                ("trigger", self.trigger)):
                for entry in data.get(key, []):
                    a, b = entry[0], entry[1]
                    self._check_layer(a)
                    self._check_layer(b)
                    target.add(self._pair(a, b))

            for entry in data.get("bodies", []):
                body_id = entry["id"]
                layer = entry["layer"]
                self._check_layer(layer)
                if body_id in self.bodies:
                    raise ValueError("duplicate body in serialized data")
                self.bodies[body_id] = {
                    "layer": layer,
                    "enabled": bool(entry.get("enabled", True)),
                    "trigger": bool(entry.get("trigger", False)),
                }

            for entry in data.get("children", []):
                parent_id = entry["parent"]
                child_id = entry["child"]
                explicit_layer = entry.get("layer")
                if parent_id not in self.bodies:
                    raise ValueError("child references unknown parent in serialized data")
                if explicit_layer is not None:
                    self._check_layer(explicit_layer)
                self.children.setdefault(parent_id, {})[child_id] = explicit_layer
