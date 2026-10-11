"""One-off splice: drop the NPMPlus-specific members from service.py.

The block replaced here (every entry-management method, the per-instance client
and the per-instance half of test_connection) now lives in the NPMPlus plugin and
is reached through the registry, so the core keeps only the generic route in.
"""

import io

PATH = (
    r"C:\Users\Brian\Documents\Scripts\reverse_proxy_automation"
    r"\reverse_proxy_automation\service.py"
)

NEW = '''    # --- destinations, one plugin at a time -----------------------------------

    def destinations(self):
        """What the page needs to render every destination: key, label, panel."""
        return self.registry.describe()

    def invoke(self, key, action, payload=None):
        """Run one destination's own panel action, naming only the destination.

        The core forwards the action and its payload without reading either, so a
        destination's vocabulary - "instance", "proxy host ID", "ownership" - never
        has to appear here. A destination that offers no panel actions is refused
        by name rather than by a missing method.
        """
        admin = self.registry.admin(key)
        if admin is None:
            raise ValueError(f"Destination '{key}' does not offer panel actions")
        return admin.invoke(action, payload if isinstance(payload, dict) else {})

    def test_connection(self, target):
        """Check the source's credentials, or whichever destination owns the name.

        The page sends a name rather than a plugin key, so every destination that
        offers panel actions is asked in turn. One that has never heard of the name
        raises UnknownTarget and the next one is asked; any other failure is a real
        one and is reported as it stands.
        """
        if str(target or "").strip().lower() == "pve":
            config = self.config_store.load()
            if not config.get("pve_local_url"):
                raise ValueError("Set the PVE Scripts Local URL first")
            password = config["pve_local_password"]
            check_secret_references({"pve_local_password": password})
            scripts = PveScriptsLocalClient(
                config["pve_local_url"],
                config["pve_local_username"],
                resolve_secret(password),
                verify_tls=_as_bool(config.get("pve_local_verify_tls"), True),
            ).installed_scripts()
            return {
                "ok": True,
                "message": f"PVE Scripts Local reachable: {len(scripts)} installed record(s).",
            }
        for plugin in self.registry.plugins():
            admin = self.registry.admin(plugin.key)
            if admin is None:
                continue
            try:
                return admin.invoke("test_connection", {"target": target})
            except UnknownTarget:
                continue
        raise ValueError(f"Unknown test target '{target}'")

'''

with io.open(PATH, encoding="utf-8") as handle:
    lines = handle.read().split("\n")

assert lines[463].strip() == "@staticmethod", lines[463]
assert lines[464].strip().startswith("def _instance_client"), lines[464]
assert lines[758].strip() == "@contextmanager", lines[758]

text = "\n".join(lines[:463] + NEW.split("\n") + lines[758:])

before = text
text = text.replace(
    '                "npmplus_seconds": destination_seconds.get("npmplus", 0),\n', ""
)
assert text != before, "npmplus_seconds timing not found"

before = text
text = text.replace(
    "            validate_config(config)\n",
    "            validate_config(config, registry=self.registry)\n",
)
assert text != before, "validate_config call not found"

before = text
text = text.replace(
    "            check_secret_references(config)\n",
    "            check_secret_references(config, registry=self.registry)\n",
)
assert text != before, "check_secret_references call not found"

with io.open(PATH, "w", encoding="utf-8", newline="\n") as handle:
    handle.write(text)

print("service.py spliced; %d lines" % len(text.split("\n")))
