"""Mixin classes that split brain.py's _exec_* methods into domain packages.

Each mixin carries a self-contained group of tool-execution methods and is
mixed into the Brain class via multiple inheritance. The Brain class supplies
all shared state (self.client, self.memory, self._productivity, …) through
its __init__, so the mixins access those attributes via self without any
additional setup.
"""
