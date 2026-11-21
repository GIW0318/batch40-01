class BonePalette:
    def __init__(self, max_bones=256, frames_in_flight=2):
        self.max_bones = max_bones
        self.frames_in_flight = frames_in_flight

    def add_bone(self, name, parent=None, bind=(0.0, 0.0, 0.0)):
        raise NotImplementedError

    def remove_bone(self, name):
        raise NotImplementedError

    def bind_material(self, material, bone):
        raise NotImplementedError

    def release_material(self, material):
        raise NotImplementedError

    def set_local_translation(self, name, translation):
        raise NotImplementedError

    def set_root_motion(self, translation):
        raise NotImplementedError

    def world_translation(self, name):
        raise NotImplementedError

    def bind_inverse_translation(self, name):
        raise NotImplementedError

    def apply_import_scale(self, scale):
        raise NotImplementedError

    def dirty_bones(self):
        raise NotImplementedError

    def build_palette(self):
        raise NotImplementedError

    def palette_for_frame(self, frame):
        raise NotImplementedError

    def submit(self, frame):
        raise NotImplementedError

    def on_gpu_consumed(self, frame):
        raise NotImplementedError

    def set_paused(self, paused):
        raise NotImplementedError

    def requires_barrier(self):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError


class InstancePaletteAllocator:
    def __init__(self, bone_count, max_instances=64):
        self.bone_count = bone_count
        self.max_instances = max_instances

    def allocate(self, instance_id):
        raise NotImplementedError

    def release(self, instance_id):
        raise NotImplementedError

    def base_of(self, instance_id):
        raise NotImplementedError
