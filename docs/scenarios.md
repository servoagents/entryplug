# Development scenario suite

Run from the source checkout with Docker and Compose already available:

```sh
./entryplug test --suite scenarios --build
./entryplug test --suite scenarios --offline --json
./entryplug test --suite scenarios --scenario mqtt --offline --json
```

The aggregate runs `zenoh`, `mqtt`, `ros2-vision`, `ros2-recovery`, and `hybrid`
sequentially. Hybrid uses the continuing Session client. Each runner retains its
existing development profile, negative cases, evidence checks and owned cleanup.
Rendering is sequential to avoid introducing timing contention between lanes.
No user account, model call, hardware or holdout is required.

Preflight resolves service images from the selected Compose files, checks Docker
and (where needed) Compose, and verifies source-labelled application images.
`--build` builds required application images with normal caches and provisions
missing pinned service images. Without it, missing or stale images fail before
any fixture starts. `--offline` forbids `--build`; fixture launches forbid implicit
pulls. First provisioning is not promised offline. Changing tracked source,
tests or documentation invalidates the conservative source digest; rerun with
`--build` before expecting a new offline pass.

The suite writes one JSON object on stdout; diagnostics go to stderr. Its
create-only `runs/scenarios-*/scenario.json` contains preflight identities and
all five lane statuses. Selected failures make the suite fail, but later lanes
still execute. Unselected lanes say `not_run`. A passing lane needs a zero exit
and a new persisted manifest that agrees with stdout and preflight identities.
Each child retains its raw evidence separately; the aggregate points to it and
embeds its manifest. A private suite directory also retains each child's stdout
and checked result. Interrupting the suite forwards SIGINT to its owned runner
and waits for that runner's cleanup before recording interruption.

Exit codes: `0` passed, `1` executed with failures, `2` invalid options or blocked
prerequisites, `130` interrupted. Existing explicit single-lane commands remain
available, including the hybrid Session and active-worker-loss options:

```sh
./entryplug test --suite scenarios --scenario hybrid --client session --fault kill-active-worker --offline --json
```

`--tier smoke` selects development execution. `--tier acceptance` currently
returns a structured prerequisite failure without launching fixtures or consuming
holdouts. Frozen profiles, the remaining required fault matrix, and strong
comparisons are still incomplete. A smoke pass does not close either the older
ROS recovery release gate or the distributed embodiment acceptance gate.

This command still requires a checkout. Installed fixture bundles, native
Borrowed Light missions, exported mixed-task qualification and external
reproduction remain separate work. See the [delivery record](implementation/mission-delivery.md)
for actual commands, outcomes and retained failures.
