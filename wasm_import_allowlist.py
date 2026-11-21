"""Sandbox policy for WebAssembly imports, memory, tables and fuel."""


class WasmTrap(Exception):
    """Raised when a module operation violates the sandbox policy."""


class FuelExhausted(WasmTrap):
    """Raised when the per-instance instruction budget is spent."""


class WasmImportAllowlist:
    """Validate and run WebAssembly modules under a two-level allowlist.

    ``allowed`` maps module names to sets of allowed field names; an import is
    accepted only when both levels match.  ``max_pages``, ``max_table``,
    ``max_instances``, ``host_pages``, ``fuel`` and ``root`` configure the
    sandbox limits.

    A module descriptor is::

        {"name": "plugin",
         "imports": [{"module": "env", "field": "log"}],
         "dependencies": [{"name": "dep", "imports": [...]}],
         "memories": [{"initial": 1, "max": 10}, ...],
         "tables": [{"entries": ["i32->i32", ...]}],
         "start_pages": 0}

    ``validate_module(module)`` returns ``{"ok", "issues"}`` and reports
    ``import_denied`` (module or field not allowed), ``nested_import_denied``
    (an import of a dependency), ``memory_limit`` (any memory's initial or
    max above ``max_pages``, or max below initial), ``table_limit`` (table
    larger than ``max_table``) and ``start_limit`` (the first memory's
    initial pages plus ``start_pages`` above ``max_pages``).

    ``instantiate(module)`` raises ``WasmTrap`` when validation fails, when
    ``max_instances`` is exceeded, or when the start function would exceed the
    memory limit.  Limits are installed before the start function runs; the
    instance records ``"log": ["limits", "start"]``.

    ``grow_memory(instance, index, delta)`` returns the previous page count
    or -1 (leaving the size unchanged) when the new size would exceed the
    declared max or ``max_pages``.  ``check_pointer(instance, index, ptr,
    length)`` raises ``WasmTrap`` unless the range lies inside the host-visible
    memory (``host_pages`` * 65536 bytes), including overflow.

    ``call_indirect(instance, table_index, expected_type)`` raises
    ``WasmTrap`` for an out-of-range table index or a signature mismatch, and
    otherwise returns the entry.  ``preopen(instance, path)`` resolves
    ``.``/``..``, raises ``WasmTrap`` when the result leaves ``root``, and
    otherwise records and returns the normalized absolute path.

    ``charge(instance, blocks=1)`` deducts one unit per basic block and raises
    ``FuelExhausted`` when the budget would go below zero, so loops are
    charged on every iteration.
    """

    def __init__(self, allowed, max_pages=256, max_table=1024,
                 max_instances=2, host_pages=16, root="/sandbox", fuel=10000):
        self.allowed = {name: set(fields) for name, fields in allowed.items()}
        self.max_pages = max_pages
        self.max_table = max_table
        self.max_instances = max_instances
        self.host_pages = host_pages
        self.root = root
        self.fuel = fuel
        self.instances = []

    def validate_module(self, module):
        raise NotImplementedError

    def instantiate(self, module):
        raise NotImplementedError

    def grow_memory(self, instance, index, delta):
        raise NotImplementedError

    def check_pointer(self, instance, index, ptr, length):
        raise NotImplementedError

    def call_indirect(self, instance, table_index, expected_type):
        raise NotImplementedError

    def preopen(self, instance, path):
        raise NotImplementedError

    def charge(self, instance, blocks=1):
        raise NotImplementedError
