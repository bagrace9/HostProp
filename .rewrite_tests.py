"""One-off rewrite: point the tests at the plugin modules and the panel actions.

The NPMPlus client, reconciler and state helpers moved into
``reverse_proxy_automation.plugins.npmplus``, and the entry-management methods the
service used to expose are now one ``service.invoke(key, action, payload)`` door.
This script moves each reference once, asserting every count so a pattern that no
longer matches stops the run instead of silently doing nothing.
"""

import io

ROOT = r"C:\Users\Brian\Documents\Scripts\reverse_proxy_automation"

PLUGIN_CLIENT = '"reverse_proxy_automation.plugins.npmplus.adapter.NpmPlusClient"'
PLUGIN_RECONCILE = '"reverse_proxy_automation.plugins.npmplus.adapter.reconcile"'

SLICE_ADOPTED = 'load_state(state_path)["destinations"]["npmplus"]["adopted"]'
SLICE_MANAGED = 'load_state(state_path)["destinations"]["npmplus"]["managed"]'


def ownership(action):
    return (
        f'invoke(service, "ownership", instance="lab", host_id="21", action="{action}")'
    )


HELPER = '''PSL_REFRESH_REQUESTS = []


def invoke(service, action, **payload):
    """Run one NPMPlus panel action the way the dashboard does: by plugin key.

    The page names the destination and an action; the core forwards the payload
    without reading a key of it. This is the same door the browser uses, so the
    tests never reach for a method the core no longer owns.
    """
    return service.invoke("npmplus", action, payload)


def guest('''

ALIASES_CALL = '''invoke(
                    service, "aliases",
                    instance="lab", host_id="21", aliases=["pictures", "gallery.example.org"],
                )'''

SERVICE_PAIRS = (
    (
        "from reverse_proxy_automation.sync import load_state",
        "from reverse_proxy_automation.statefile import load_state",
        1,
    ),
    (
        '"reverse_proxy_automation.service.NpmPlusClient"',
        PLUGIN_CLIENT,
        25,
    ),
    (
        '"reverse_proxy_automation.service.reconcile"',
        PLUGIN_RECONCILE,
        1,
    ),
    ("PSL_REFRESH_REQUESTS = []\n\n\ndef guest(", HELPER, 1),
    ('service.list_entries()', 'invoke(service, "list_entries")', 5),
    (ownership("adopt"), ownership("adopt"), 0),
    ('service.set_ownership("lab", "21", "adopt")', ownership("adopt"), 3),
    ('service.set_ownership("lab", "21", "release")', ownership("release"), 1),
    ('service.set_ownership("lab", "21", "promote")', ownership("promote"), 1),
    (
        'service.set_entry_aliases(\n'
        '                    "lab", "21", ["pictures", "gallery.example.org"]\n'
        '                )',
        ALIASES_CALL,
        1,
    ),
    (
        'service.set_entry_aliases("lab", "21", ["media-service"])',
        'invoke(service, "aliases", instance="lab", host_id="21", aliases=["media-service"])',
        1,
    ),
    (
        'service.delete_entry("lab", "21")',
        'invoke(service, "delete", instance="lab", host_id="21")',
        2,
    ),
    (
        'service.delete_entry("dmz", "1")',
        'invoke(service, "delete", instance="dmz", host_id="1")',
        1,
    ),
    (
        'service.set_entry_port("lab", "21", 8090)',
        'invoke(service, "port", instance="lab", host_id="21", port=8090)',
        1,
    ),
    (
        'service.set_entry_port("lab", "21", "")',
        'invoke(service, "port", instance="lab", host_id="21", port="")',
        1,
    ),
    (
        'service.set_entry_port("lab", "21", port)',
        'invoke(service, "port", instance="lab", host_id="21", port=port)',
        1,
    ),
    ('load_state(state_path)["adopted"]', SLICE_ADOPTED, 2),
    ('load_state(state_path)["managed"]', SLICE_MANAGED, 3),
)

CLIENTS_PAIRS = (
    (
        "    NpmPlusClient,\n",
        "",
        1,
    ),
    (
        ")\n\n\nclass BrokenOpener:",
        ")\n\n# The NPMPlus API client moved into the destination package that owns it. It is\n"
        "# still covered here, where the fake HTTP opener these tests share lives.\n"
        "from reverse_proxy_automation.plugins.npmplus.client import NpmPlusClient\n"
        "\n\nclass BrokenOpener:",
        1,
    ),
)


def rewrite(path, pairs):
    with io.open(path, encoding="utf-8") as handle:
        text = handle.read()
    for old, new, expected in pairs:
        found = text.count(old)
        assert found == expected, f"{path}: found {found}, expected {expected} for {old!r}"
        text = text.replace(old, new)
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    print(f"{path}: {len(pairs)} pattern(s) applied")


rewrite(f"{ROOT}\\tests\\test_service.py", SERVICE_PAIRS)
rewrite(f"{ROOT}\\tests\\test_clients.py", CLIENTS_PAIRS)
