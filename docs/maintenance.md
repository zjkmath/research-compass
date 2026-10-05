# Updating evidence

```sh
python compass.py --data-dir ../research-compass-state refresh-due --dry-run --limit 25
```

This reports a bounded, prioritized queue without fetching or approving facts. Source permission, policy freshness, supported adapter and explicit execution are required before a live request. Empty source configuration in the demo grants no collection permission. Failed reads do not automatically close an opportunity. Approved/rejected proposals and new scans preserve historical observations.

No OS scheduler is installed. Operators may review a printed scheduler template and choose scheduling separately; automatic and human-review work must remain distinct. Synthetic seed timestamps deliberately show stale evidence and conflicts. Do not refresh their clocks to make them appear real or current.

Keep private operational data separate from public source. For upstream fixes, prepare a minimal sanitized patch and synthetic regression. Never synchronize the private production checkout wholesale to this repository. Software `upgrade` migrates schema without reloading the demo or replacing facts; reviewed factual imports require an explicit operator action.
