import json


class _Node:
    def __init__(self, name, parent, translation, scale):
        self.name = name
        self.parent = parent
        self.children = []
        self.translation = tuple(float(v) for v in translation)
        self.scale = tuple(float(v) for v in scale)
        self.visible = True
        self.dirty = True
        self.world_cache = None


class TransformGraph:
    def __init__(self):
        self._nodes = {}
        self._roots = []
        self._paused = False
        self._recomputed = 0

    def add_node(self, name, parent=None, translation=(0.0, 0.0, 0.0),
                 scale=(1.0, 1.0, 1.0)):
        if name in self._nodes:
            raise ValueError("node already exists: %r" % (name,))
        parent_name = parent
        if parent_name is not None and parent_name not in self._nodes:
            raise KeyError(parent_name)
        node = _Node(name, parent_name, translation, scale)
        self._nodes[name] = node
        if parent_name is None:
            self._roots.append(name)
        else:
            self._nodes[parent_name].children.append(name)
        return name

    def remove_node(self, name):
        node = self._nodes[name]
        subtree = self._collect_subtree(name)
        if node.parent is None:
            self._roots.remove(name)
        else:
            self._nodes[node.parent].children.remove(name)
        for child_name in subtree:
            del self._nodes[child_name]

    def reparent(self, name, new_parent):
        node = self._nodes[name]
        if new_parent is not None:
            if new_parent not in self._nodes:
                raise KeyError(new_parent)
            if new_parent == name or new_parent in self._collect_subtree(name):
                raise ValueError("reparent would create a cycle")

        old_world = self.world_translation(name)

        if node.parent is None:
            self._roots.remove(name)
        else:
            self._nodes[node.parent].children.remove(name)

        node.parent = new_parent
        if new_parent is None:
            self._roots.append(name)
            parent_world = (0.0, 0.0, 0.0)
        else:
            self._nodes[new_parent].children.append(name)
            parent_world = self.world_translation(new_parent)

        node.translation = tuple(
            old_world[i] - parent_world[i] for i in range(3)
        )
        self._mark_subtree_dirty(node)

    def set_translation(self, name, translation):
        node = self._nodes[name]
        node.translation = tuple(float(v) for v in translation)
        self._mark_subtree_dirty(node)

    def set_scale(self, name, scale):
        node = self._nodes[name]
        node.scale = tuple(float(v) for v in scale)
        self._mark_subtree_dirty(node)

    def set_visible(self, name, visible):
        self._nodes[name].visible = bool(visible)

    def _collect_subtree(self, name):
        result = []
        stack = [name]
        while stack:
            current = stack.pop()
            result.append(current)
            for child in reversed(self._nodes[current].children):
                stack.append(child)
        return result

    def _mark_subtree_dirty(self, node):
        stack = [node]
        while stack:
            current = stack.pop()
            current.dirty = True
            for child_name in current.children:
                stack.append(self._nodes[child_name])

    def _walk(self):
        result = []
        stack = list(reversed(self._roots))
        while stack:
            name = stack.pop()
            result.append(name)
            node = self._nodes[name]
            for child in reversed(node.children):
                stack.append(child)
        return result

    def _world_translation_of(self, name):
        chain = []
        current = name
        while current is not None:
            chain.append(current)
            current = self._nodes[current].parent
        total = [0.0, 0.0, 0.0]
        for current in reversed(chain):
            translation = self._nodes[current].translation
            for i in range(3):
                total[i] += translation[i]
        return tuple(total)

    def world_translation(self, name):
        if name not in self._nodes:
            raise KeyError(name)
        return self._world_translation_of(name)

    def updated_world(self, name):
        node = self._nodes[name]
        if node.world_cache is None:
            node.world_cache = self._world_translation_of(name)
        return node.world_cache

    def mark_dirty(self, name):
        self._mark_subtree_dirty(self._nodes[name])

    def dirty_nodes(self):
        return [name for name in self._walk() if self._nodes[name].dirty]

    def update(self):
        if self._paused:
            return []
        recomputed = []
        for name in self._walk():
            node = self._nodes[name]
            if not node.dirty:
                continue
            if node.parent is None:
                world = node.translation
            else:
                parent_world = self.updated_world(node.parent)
                world = tuple(
                    parent_world[i] + node.translation[i] for i in range(3)
                )
            node.world_cache = world
            node.dirty = False
            recomputed.append(name)
        self._recomputed += len(recomputed)
        return recomputed

    def set_paused(self, paused):
        self._paused = bool(paused)

    def visible_nodes(self):
        result = []
        for name in self._walk():
            node = self._nodes[name]
            if node.parent is None:
                parent_visible = True
            else:
                parent_visible = node.parent in result
            if node.visible and parent_visible:
                result.append(name)
        return result

    def traversal_order(self):
        return self._walk()

    def iter_snapshot(self):
        return self._walk()

    def serialize(self):
        records = []
        for name in self._walk():
            node = self._nodes[name]
            records.append({
                "name": node.name,
                "parent": node.parent,
                "translation": list(node.translation),
                "scale": list(node.scale),
                "visible": node.visible,
            })
        return json.dumps(
            {"nodes": records},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @classmethod
    def deserialize(cls, text):
        data = json.loads(text)
        graph = cls()
        for record in data["nodes"]:
            graph.add_node(
                record["name"],
                parent=record["parent"],
                translation=tuple(record["translation"]),
                scale=tuple(record["scale"]),
            )
            if not record.get("visible", True):
                graph.set_visible(record["name"], False)
        return graph

    def snapshot(self):
        return {name: self._world_translation_of(name)
                for name in self._walk()}

    def instance_translation(self, prototype, override):
        node = self._nodes[prototype]
        base = self._world_translation_of(node.parent) if node.parent \
            else (0.0, 0.0, 0.0)
        return tuple(base[i] + float(override[i]) for i in range(3))

    def normal_sign(self, name):
        sign = 1.0
        current = name
        while current is not None:
            for component in self._nodes[current].scale:
                if component < 0.0:
                    sign = -sign
            current = self._nodes[current].parent
        return sign

    def rebase_origin(self, offset):
        shift = tuple(float(v) for v in offset)
        for name in self._roots:
            node = self._nodes[name]
            node.translation = tuple(
                node.translation[i] - shift[i] for i in range(3)
            )
            self._mark_subtree_dirty(node)

    def stats(self):
        return {
            "node_count": len(self._nodes),
            "recomputed": self._recomputed,
            "paused": self._paused,
        }
