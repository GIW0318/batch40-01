class BonePalette:
    def __init__(self, max_bones=256, frames_in_flight=2):
        if max_bones <= 0:
            raise ValueError("max_bones must be positive")
        if frames_in_flight <= 0:
            raise ValueError("frames_in_flight must be positive")
        self.max_bones = max_bones
        self.frames_in_flight = frames_in_flight
        self._bones = {}
        self._children = {}
        self._indices = {}
        self._free_indices = []
        self._next_index = 0
        self._materials = {}
        self._dirty = set()
        self._root_motion = (0.0, 0.0, 0.0)
        self._paused = False
        self._pending = {}
        self._snapshots = {}
        self._uploaded = 0
        self._blocked_submissions = 0

    def add_bone(self, name, parent=None, bind=(0.0, 0.0, 0.0)):
        if name in self._bones:
            raise ValueError("bone %r already exists" % name)
        if parent is not None and parent not in self._bones:
            raise KeyError(parent)
        if len(self._bones) >= self.max_bones:
            raise ValueError("bone palette capacity exceeded")

        if self._free_indices:
            index = self._free_indices.pop(0)
        else:
            index = self._next_index
            self._next_index += 1

        bind = tuple(float(value) for value in bind)
        self._bones[name] = {
            "index": index,
            "parent": parent,
            "local": (0.0, 0.0, 0.0),
            "bind": bind,
        }
        self._indices[name] = index
        self._children[name] = []
        if parent is not None:
            self._children[parent].append(name)
        self._dirty.add(name)
        return index

    def remove_bone(self, name):
        bone = self._bone(name)
        if name in self._materials.values():
            raise ValueError("bone %r is still referenced by a material" % name)

        parent = bone["parent"]
        moved_children = list(self._children[name])
        if parent is not None:
            self._children[parent].remove(name)

        index = self._indices.pop(name)
        del self._bones[name]
        del self._children[name]
        self._dirty.discard(name)
        self._free_indices.append(index)
        self._free_indices.sort()

        for child_name in moved_children:
            child = self._bones[child_name]
            child["parent"] = parent
            if parent is not None:
                self._children[parent].append(child_name)
            self._mark_subtree_dirty(child_name)

    def bind_material(self, material, bone):
        self._bone(bone)
        self._materials[material] = bone

    def release_material(self, material):
        if material not in self._materials:
            raise KeyError(material)
        del self._materials[material]

    def set_local_translation(self, name, translation):
        self._bone(name)
        self._bones[name]["local"] = tuple(float(value) for value in translation)
        self._mark_subtree_dirty(name)

    def set_root_motion(self, translation):
        self._root_motion = tuple(float(value) for value in translation)
        self._dirty.update(self._bones)

    def world_translation(self, name):
        self._bone(name)
        values = []
        current = name
        while current is not None:
            values.append(self._bones[current]["local"])
            current = self._bones[current]["parent"]
        result = list(self._root_motion)
        for local in reversed(values):
            for axis in range(3):
                result[axis] += local[axis]
        return tuple(result)

    def bind_inverse_translation(self, name):
        bind = self._bone(name)["bind"]
        return tuple(-value for value in bind)

    def apply_import_scale(self, scale):
        scale = float(scale)
        for name in self._bones:
            bind = self._bones[name]["bind"]
            self._bones[name]["bind"] = tuple(value * scale for value in bind)
        self._dirty.update(self._bones)

    def dirty_bones(self):
        return [name for name in self._bones if name in self._dirty]

    def build_palette(self):
        return {
            name: self.world_translation(name)
            for name in self._bones
        }

    def palette_for_frame(self, frame):
        if frame not in self._snapshots:
            raise KeyError(frame)
        return dict(self._snapshots[frame])

    def submit(self, frame):
        if self._paused:
            self._blocked_submissions += 1
            return []
        if frame in self._pending:
            raise ValueError("frame %r is already in flight" % frame)
        if len(self._pending) >= self.frames_in_flight:
            self._blocked_submissions += 1
            return []
        if not self._dirty:
            return []

        uploaded = [
            self._indices[name]
            for name in self.dirty_bones()
        ]
        slot = len(self._pending) % self.frames_in_flight
        snapshot = self.build_palette()
        self._pending[frame] = {
            "slot": slot,
            "bones": set(uploaded),
        }
        self._snapshots[frame] = snapshot
        self._dirty.clear()
        self._uploaded += len(uploaded)
        return uploaded

    def on_gpu_consumed(self, frame):
        if frame not in self._pending:
            raise KeyError(frame)
        del self._pending[frame]
        del self._snapshots[frame]

    def set_paused(self, paused):
        self._paused = bool(paused)

    def requires_barrier(self):
        return bool(self._pending)

    def stats(self):
        return {
            "uploaded": self._uploaded,
            "blocked_submissions": self._blocked_submissions,
            "dirty": len(self._dirty),
            "in_flight": len(self._pending),
            "barrier": self.requires_barrier(),
        }

    def _mark_subtree_dirty(self, name):
        self._dirty.add(name)
        for child in self._children[name]:
            self._mark_subtree_dirty(child)

    def _bone(self, name):
        try:
            return self._bones[name]
        except KeyError:
            raise KeyError(name)


class InstancePaletteAllocator:
    def __init__(self, bone_count, max_instances=64):
        if bone_count <= 0:
            raise ValueError("bone_count must be positive")
        if max_instances <= 0:
            raise ValueError("max_instances must be positive")
        self.bone_count = bone_count
        self.max_instances = max_instances
        self._bases = {}
        self._free_slots = []
        self._next_slot = 0

    def allocate(self, instance_id):
        if instance_id in self._bases:
            raise ValueError("instance %r is already allocated" % instance_id)
        if len(self._bases) >= self.max_instances:
            raise ValueError("instance palette capacity exceeded")

        if self._free_slots:
            slot = self._free_slots.pop(0)
        else:
            slot = self._next_slot
            self._next_slot += 1

        base = slot * self.bone_count
        self._bases[instance_id] = base
        return base

    def release(self, instance_id):
        if instance_id not in self._bases:
            raise KeyError(instance_id)
        base = self._bases.pop(instance_id)
        self._free_slots.append(base // self.bone_count)
        self._free_slots.sort()

    def base_of(self, instance_id):
        if instance_id not in self._bases:
            raise KeyError(instance_id)
        return self._bases[instance_id]
