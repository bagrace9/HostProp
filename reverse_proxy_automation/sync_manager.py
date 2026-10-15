"""Management of the synchronization lifecycle and watcher loop.
"""

import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import validate_core_config, check_source_secret_references, destination_enabled, destination_applies

class SyncManager:
    """Orchestrates the synchronization runs and the watcher thread.
    """
    def __init__(self, sync_service):
        self.service = sync_service
        self.lock = sync_service.lock
        self.run_lock = sync_service.run_lock
        self.wake = sync_service.wake
        self.stop_event = sync_service.stop_event

    def _run_loop(self):
        """The background watcher loop.
        """
        while not self.stop_event.is_set():
            config = self.service.config_store.load()
            if not self.service.config_store.lock.acquire(blocking=False):
                # Avoid fighting with the webapp for the config file
                self.wake.wait(timeout=1)
                continue

            try:
                if not self.service.config_store.lock.acquire(blocking=False):
                    continue
                # Wait for a config load and validate
                # (The a-sync nature of the loop means we only check interval periodically)
                interval = config.get("poll_interval_seconds")
                if not config.get("watch_enabled") or interval is None:
                    self.wake.wait(timeout=60)
                    continue
                
                # Standard interval check
                # (This logic is simplified from the original service.py loop)
                self.refresh_source()
                if self.service.config_store.load().get("auto_apply"):
                    self.auto_apply()
            except Exception:
                # The loop must never die
                pass
            finally:
                # Release lock if we acquired it
                pass 
            
            self.wake.wait(timeout=interval if interval else 60)

    def refresh_source(self, refresh_lxc_config=False, wait=True):
        """Read PSL once and keep the records for every destination to plan against."""
        if not self.run_lock.acquire(blocking=wait):
            return self.service._busy("A sync run is already in progress; the source was not read again.")
        try:
            try:
                config = self.service.config_store.load()
                validate_core_config(config)
                check_source_secret_references(config)
            except Exception as error:
                return self.service._fail(error)
            
            self.service.executor.read_source(config, refresh_lxc_config)
            return self.service.get_status()
        finally:
            self.run_lock.release()

    def run_once(self, apply_changes=False, refresh_lxc_config=False):
        if not self.run_lock.acquire(blocking=refresh_lxc_config):
            return self.service._busy(
                "A sync run is already in progress; this request did not start a new preview or apply."
            )
        try:
            return self._run_once(apply_changes, refresh_lxc_config)
        finally:
            self.run_lock.release()

    def _run_once(self, apply_changes, refresh_lxc_config=False):
        try:
            config = self.service.config_store.load()
            from .config import validate_config, check_secret_references
            validate_config(config, registry=self.service.registry)
            check_secret_references(config, registry=self.service.registry)
            
            guests, psl_seconds = self.service.executor.read_source(config, refresh_lxc_config)
            if guests is None:
                return self.service.get_status()
            
            active = [
                adapter for adapter in self.service.destination_adapters
                if destination_enabled(config, adapter.key)
            ]
            return self.service.executor.run_destinations(config, guests, active, apply_changes, psl_seconds)
        except Exception as error:
            return self.service._fail(error)

    def auto_apply(self, config=None):
        if not self.run_lock.acquire(blocking=False):
            return self.service._busy("A sync run is already in progress; this background apply was skipped.")
        try:
            config = config if isinstance(config, dict) else self.service.config_store.load()
            validate_core_config(config)
            appliers = [
                adapter for adapter in self.service.destination_adapters
                if destination_applies(config, adapter.key)
            ]
            if not appliers:
                return self.service.get_status()
            
            snapshot = self.service.executor.source_snapshot()
            if snapshot is None:
                guests, psl_seconds = self.service.executor.read_source(config, True)
                if guests is None:
                    return self.service.get_status()
            else:
                guests = snapshot["guests"]
                psl_seconds = snapshot["seconds"]
                
            return self.service.executor.run_destinations(config, guests, appliers, True, psl_seconds)
        except Exception as error:
            return self.service._fail(error)
