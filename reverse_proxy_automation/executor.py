"""Logic for executing synchronization runs between the source and destinations.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from .clients import PveScriptsLocalClient
from .integrations import PslSourceAdapter
from .settings import resolve_secret, as_bool as _as_bool
from .statefile import load_state, save_state
from .config import CHANGE_STATUSES, ATTENTION_STATUSES

LOGGER = logging.getLogger(__name__)

class SyncExecutor:
    """Handles the actual read and plan/apply cycle for destinations.
    """
    def __init__(self, sync_service):
        self.service = sync_service

    def read_source(self, config, refresh_lxc_config=False):
        """Read PSL once and publish the snapshot. Returns (guests, seconds)."""
        self.service._set_status(
            state="checking",
            message=(
                "Refreshing guest configuration from Proxmox through PVE Scripts Local."
                if refresh_lxc_config
                else "Reading installed records from PVE Scripts Local."
            ),
            guest_count=0,
            timings={},
        )
        try:
            psl_started = perf_counter()
            guests = self.service.source_adapter.read(
                config,
                refresh_lxc_config=refresh_lxc_config,
                progress_callback=(
                    lambda current, total, script_id, container_id: self.service._set_status(
                        message=(
                            f"Refreshing PSL record {current} of {total} "
                            f"(record {script_id}, container {container_id}): "
                            "reading current Proxmox config and detecting the current Web UI endpoint."
                        )
                    )
                ) if refresh_lxc_config else None,
            )
            psl_seconds = perf_counter() - psl_started
        except Exception as error:
            with self.service.lock:
                self.service.snapshot = None
                self.service.results.clear()
            LOGGER.warning("Reading the source failed: %s", error)
            self.service._fail(error)
            return None, None

        now = datetime.now(timezone.utc).isoformat()
        with self.service.lock:
            self.service.snapshot = {
                "guests": list(guests),
                "read_at": now,
                "seconds": round(psl_seconds, 3),
                "refresh_lxc_config": bool(refresh_lxc_config),
            }
        self.service._set_status(
            state="ok",
            message=(
                f"Read {len(guests)} guest record(s) from PSL in {psl_seconds:.2f}s; "
                "building destination plans."
            ),
            last_check=now,
            guest_count=len(guests),
            timings={"psl_read_seconds": round(psl_seconds, 3), "source_records": len(guests)},
        )
        return guests, psl_seconds

    def run_destinations(self, config, guests, active, apply_changes=False, psl_seconds=0.0):
        """Plan and apply for the destinations named, recording what each one did."""
        state = load_state(self.service.state_path)
        failures = []
        planning_seconds = 0.0
        destination_seconds = {}
        guest_counts = []
        self.service._set_status(state="checking", message="Building destination plans.")
        
        try:
            for adapter in active:
                try:
                    self.service._validate_destination(config, adapter.key)
                    planning_started = perf_counter()
                    destination_plan = adapter.plan(guests, config)
                    destination_planning = perf_counter() - planning_started
                    self.service._set_status(message=f"Planned {adapter.label}; applying.")
                    destination_started = perf_counter()
                    destination_rows = adapter.apply(
                        destination_plan,
                        config,
                        state,
                        apply_changes=apply_changes,
                    )
                except Exception as error:
                    if len(active) == 1:
                        raise
                    LOGGER.exception("Destination %s failed", adapter.label)
                    failures.append(f"{adapter.label}: {error}")
                    self.service._record_destination_result(
                        adapter,
                        state="error",
                        message=str(error),
                        apply_changes=apply_changes,
                        guest_count=len(guests),
                    )
                    continue
                
                planning_seconds += destination_planning
                destination_seconds[adapter.key] = round(
                    perf_counter() - destination_started, 3
                )
                for row in destination_rows:
                    if row[0] == "OK":
                        continue
                    LOGGER.info(
                        "Sync result: destination=%s status=%s target=%s resource=%s",
                        adapter.label, row[0], row[1], row[2],
                    )
                guest_count = destination_plan.get("containers", len(guests))
                guest_counts.append(guest_count)
                self.service._record_destination_result(
                    adapter,
                    state="ok",
                    message=self.service._destination_message(destination_rows, destination_plan, apply_changes),
                    apply_changes=apply_changes,
                    guest_count=guest_count,
                    rows=destination_rows,
                    plan=destination_plan,
                )
        finally:
            if apply_changes:
                save_state(self.service.state_path, state)

        now = datetime.now(timezone.utc).isoformat()
        results = self.service._results_in_order()
        planned = [row for result in results.values() for row in result.get("planned", [])]
        skipped = [row for result in results.values() for row in result.get("skipped", [])]
        mode = "Applied" if apply_changes else "Previewed"
        
        # helper for counting actionable changes
        def _actionable(rows, status_key):
            return sum(row[status_key] in CHANGE_STATUSES for row in rows)
            
        message = (
            f"{mode} {_actionable(planned, 'status')} change(s); "
            f"{len(skipped)} item(s) need attention."
        )
        if not active:
            message = f"Read {len(guests)} guest record(s) from PSL; no destination is enabled."
        if failures:
            message = f"{'; '.join(failures)}. {message}"
            
        now = datetime.now(timezone.utc).isoformat()
        results = self.service._results_in_order()
        planned = [row for result in results.values() for row in result.get("planned", [])]
        skipped = [row for result in results.values() for row in result.get("skipped", [])]
        mode = "Applied" if apply_changes else "Previewed"
        
        # helper for counting actionable changes
        def _actionable(rows, status_key):
            return sum(row[status_key] in CHANGE_STATUSES for row in rows)
            
        message = (
            f"{mode} {_actionable(planned, 'status')} change(s); "
            f"{len(skipped)} item(s) need attention."
        )
        if not active:
            message = f"Read {len(guests)} guest record(s) from PSL; no destination is enabled."
        if failures:
            message = f"{'; '.join(failures)}. {message}"
            
        run_metrics = {
            "state": "error" if failures else "ok",
            "message": message,
            "last_check": now,
            "last_apply": now if apply_changes else self.service.get_status().get("last_apply"),
            "guest_count": max(guest_counts, default=len(guests)),
            "timings": {
                "psl_read_seconds": round(psl_seconds, 3),
                "planning_seconds": round(planning_seconds, 3),
                "destinations_seconds": destination_seconds,
                "source_records": len(guests),
            },
        }
        self.service._set_status(**run_metrics)
        
        # Persist metrics to state for future restarts
        state = load_state(self.service.state_path)
        state["_last_run_metrics"] = run_metrics
        save_state(self.service.state_path, state)
        
        return self.service.get_status()
