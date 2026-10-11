"""One-off splice: strip the last NPMPlus specifics out of the core modules.

``clients.py`` keeps the transport every destination may use and loses the
NPMPlus API client and its certificate choice (both now in the plugin), and
``integrations.py`` loses the adapter class that moved to the plugin.
"""

import io

ROOT = r"C:\Users\Brian\Documents\Scripts\reverse_proxy_automation\reverse_proxy_automation"


def rewrite(path, transform):
    with io.open(path, encoding="utf-8") as handle:
        original = handle.read()
    updated = transform(original)
    with io.open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(updated)
    return len(original.split("\n")), len(updated.split("\n"))


def strip_clients(text):
    head, marker, _ = text.partition("\n\nclass NpmPlusClient:")
    assert marker, "NpmPlusClient class not found in clients.py"
    text = head.rstrip("\n") + "\n"
    removed = "from datetime import datetime, timezone\n"
    assert removed in text, "datetime import not found in clients.py"
    text = text.replace(removed, "")
    for name in ("NpmPlusClient", "select_certificate", "select_shared_certificate", "_parse_expiry"):
        assert name not in text, f"{name} still in clients.py"
    return text


def strip_integrations(text):
    head, marker, _ = text.partition("\n\nclass NpmPlusDestinationAdapter:")
    assert marker, "NpmPlusDestinationAdapter not found in integrations.py"
    text = head.rstrip("\n") + "\n"
    text = text.replace(
        "\nfrom .planner import parse_exclusions, plan_hosts\n\n",
        "\n",
        1,
    )
    assert "plan_hosts" not in text, "plan_hosts still imported in integrations.py"
    return text


print("clients.py %d -> %d lines" % rewrite(f"{ROOT}\\clients.py", strip_clients))
print("integrations.py %d -> %d lines" % rewrite(f"{ROOT}\\integrations.py", strip_integrations))
