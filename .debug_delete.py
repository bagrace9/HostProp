"""Throwaway: reproduce one entry-management test and dump the state file it writes."""

import sys
import tempfile
from unittest import mock

ROOT = r"C:\Users\Brian\Documents\Scripts\reverse_proxy_automation"
sys.path.insert(0, ROOT)
sys.path.insert(0, ROOT + r"\tests")

import test_service as T  # noqa: E402

registry = {"lab NPMPlus": T.FakeNpmPlus("lab NPMPlus", "https://lab.example.net:81")}
registry["lab NPMPlus"].hosts["21"] = {
    "domain_names": ["media-service.example.net", "photos.example.net"],
    "forward_scheme": "http",
    "forward_host": "media-service.public.example.net",
    "forward_port": 8081,
}


def factory(name, url, identity="", secret="", verify_tls=True):
    return registry[name]


case = T.EntryManagementTests("test_deleting_an_entry_removes_it_and_its_recorded_ownership")
with tempfile.TemporaryDirectory() as directory, \
        mock.patch("reverse_proxy_automation.plugins.npmplus.adapter.NpmPlusClient", factory):
    service, state_path = case.build(
        directory,
        {"lab": {"url": "https://lab.example.net:81"}},
        managed={"lab:media-service.example.net": "21"},
    )
    print("BEFORE:", state_path.read_text())
    try:
        result = service.invoke("npmplus", "delete", {"instance": "lab", "host_id": "21"})
        print("RESULT:", result)
    except Exception as error:  # noqa: BLE001 - a debug run must show whatever happens
        print("RAISED:", type(error).__name__, error)
    finally:
        print("AFTER:", state_path.read_text())
        service.stop()
    print("FINAL:", state_path.read_text())
