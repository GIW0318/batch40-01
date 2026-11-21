"""Sandbox policy for WebAssembly imports, memory, tables and fuel."""

import os


PAGE_SIZE = 65536


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

    def _import_allowed(self, imp):
        fields = self.allowed.get(imp.get("module"))
        return fields is not None and imp.get("field") in fields

    def _check_imports(self, imports, issues, nested):
        code = "nested_import_denied" if nested else "import_denied"
        for imp in imports or []:
            if not self._import_allowed(imp):
                issues.append({
                    "code": code,
                    "module": imp.get("module"),
                    "field": imp.get("field"),
                })

    def _check_dependencies(self, dependencies, issues):
        for dependency in dependencies or []:
            self._check_imports(
                dependency.get("imports", []), issues, nested=True
            )
            self._check_dependencies(
                dependency.get("dependencies", []), issues
            )

    def validate_module(self, module):
        issues = []

        self._check_imports(module.get("imports", []), issues, nested=False)
        self._check_dependencies(module.get("dependencies", []), issues)

        for index, memory in enumerate(module.get("memories", [])):
            initial = memory.get("initial", 0)
            maximum = memory.get("max", initial)
            if (
                initial > self.max_pages
                or maximum > self.max_pages
                or maximum < initial
            ):
                issues.append({
                    "code": "memory_limit",
                    "index": index,
                    "initial": initial,
                    "max": maximum,
                    "limit": self.max_pages,
                })

        for index, table in enumerate(module.get("tables", [])):
            size = len(table.get("entries", []))
            if size > self.max_table:
                issues.append({
                    "code": "table_limit",
                    "index": index,
                    "size": size,
                    "limit": self.max_table,
                })

        memories = module.get("memories", [])
        start_pages = module.get("start_pages", 0)
        first_initial = memories[0].get("initial", 0) if memories else 0
        if first_initial + start_pages > self.max_pages:
            issues.append({
                "code": "start_limit",
                "start_pages": start_pages,
                "limit": self.max_pages,
            })

        return {"ok": not issues, "issues": issues}

    def instantiate(self, module):
        result = self.validate_module(module)
        if not result["ok"]:
            raise WasmTrap("module failed validation: %s" % result["issues"])
        if len(self.instances) >= self.max_instances:
            raise WasmTrap("instance limit exceeded")

        memories = module.get("memories", [])
        start_pages = module.get("start_pages", 0)
        first_initial = memories[0].get("initial", 0) if memories else 0
        if first_initial + start_pages > self.max_pages:
            raise WasmTrap("start function would exceed memory limit")

        instance = {
            "name": module.get("name"),
            "module": module,
            "log": [],
            "memories": [
                {"initial": memory.get("initial", 0),
                 "max": memory.get("max", memory.get("initial", 0)),
                 "pages": memory.get("initial", 0)}
                for memory in memories
            ],
            "tables": [
                {"entries": list(table.get("entries", []))}
                for table in module.get("tables", [])
            ],
            "preopens": [],
            "fuel": self.fuel,
        }

        instance["log"].append("limits")
        self.instances.append(instance)
        instance["log"].append("start")
        return instance

    def grow_memory(self, instance, index, delta):
        if index < 0 or index >= len(instance["memories"]):
            raise WasmTrap("memory index out of range")
        if delta < 0:
            raise WasmTrap("negative memory growth")
        memory = instance["memories"][index]
        current = memory["pages"]
        target = current + delta
        if target > memory["max"] or target > self.max_pages:
            return -1
        memory["pages"] = target
        return current

    def check_pointer(self, instance, index, ptr, length):
        if index < 0 or index >= len(instance["memories"]):
            raise WasmTrap("memory index out of range")
        if length < 0 or ptr < 0:
            raise WasmTrap("pointer range out of bounds")
        bound = self.host_pages * PAGE_SIZE
        if ptr > bound or ptr + length > bound or ptr + length < ptr:
            raise WasmTrap("pointer range out of bounds")
        return True

    def call_indirect(self, instance, table_index, expected_type):
        if table_index < 0 or table_index >= len(instance["tables"]):
            raise WasmTrap("table index out of range")
        table = instance["tables"][table_index]
        entries = table["entries"]
        entry_index = table.get("index", 0)
        if entry_index < 0 or entry_index >= len(entries):
            raise WasmTrap("table element index out of range")
        entry = entries[entry_index]
        if entry != expected_type:
            raise WasmTrap("indirect call signature mismatch")
        return entry

    def preopen(self, instance, path):
        root = os.path.realpath(self.root)
        candidate = os.path.realpath(os.path.join(root, path))
        if candidate != root and not candidate.startswith(root + os.sep):
            raise WasmTrap("preopen path escapes sandbox root")
        instance["preopens"].append(candidate)
        return candidate

    def charge(self, instance, blocks=1):
        if blocks < 0:
            raise WasmTrap("negative fuel charge")
        instance["fuel"] -= blocks
        if instance["fuel"] < 0:
            raise FuelExhausted("instruction budget exhausted")
        return instance["fuel"]
