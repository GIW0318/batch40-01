"""Particle depth sorting batch.

Contract used by the tests
--------------------------
* ``__init__(sort_budget_ms=1.0)``.
* ``depth(particle, camera)``: camera is (x,y,z) looking down -z.  The
  particle is a sphere at pos (x,y,z) with size d.  Depth is the distance
  from the camera plane to the nearest point of the bounding sphere:
  camera_z - (z + d/2).  Larger particles are therefore nearer.
* ``sort_particles(particles, camera, budget_ms=None)`` returns {"order",
  "sorted", "budget_exceeded", "cost_ms"}.  Sorting is global (not per
  emitter) and back-to-front: larger depth first.  Equal depths are ordered
  by ascending particle id.  Cost is len(particles) * 0.001 ms.  When the
  budget is exceeded no sorting is done: order is the input order, sorted
  False and budget_exceeded True.  budget_ms defaults to the constructor
  budget.
* ``visible(particle, near)`` returns True when z + size/2 > near, even
  when the centre z is behind the near plane.
* ``depth_write(particle)`` returns True only when alpha >= 1.0;
  transparent particles never write depth.
* ``acquire(emitter)`` returns a clean pooled particle {"id", "emitter",
  "alive": False, "sort_index": None}; ``release(particle)`` clears
  sort_index and marks it dead.  Reuse never leaks a previous sort index.
* ``generate_chunks(chunks)`` concatenates all per-worker lists and sorts by
  ascending id, independent of chunk order.
* ``required_index_bits(count)`` returns max(1, (count-1).bit_length()).
* ``encode_index(index, bits)`` returns index when it fits in bits,
  otherwise raises OverflowError (never wraps).
"""


class ParticleDepthBatch:
    def __init__(self, sort_budget_ms=1.0):
        self.sort_budget_ms = sort_budget_ms

    def depth(self, particle, camera):
        raise NotImplementedError

    def sort_particles(self, particles, camera, budget_ms=None):
        raise NotImplementedError

    def visible(self, particle, near):
        raise NotImplementedError

    def depth_write(self, particle):
        raise NotImplementedError

    def acquire(self, emitter):
        raise NotImplementedError

    def release(self, particle):
        raise NotImplementedError

    def generate_chunks(self, chunks):
        raise NotImplementedError

    def required_index_bits(self, count):
        raise NotImplementedError

    def encode_index(self, index, bits):
        raise NotImplementedError
