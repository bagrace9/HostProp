"""NPMPlus, shipped as a destination plugin.

The core imports this module by name from its built-in list and takes the single
object it exports. Everything NPMPlus needs - its settings, its clients, its
records in the state file, its dashboard panel - lives in this package, so the
core never refers to it again.
"""

from .plugin import NpmPlusPlugin


#: The destination the core registers.
PLUGIN = NpmPlusPlugin()


__all__ = ["PLUGIN", "NpmPlusPlugin"]
