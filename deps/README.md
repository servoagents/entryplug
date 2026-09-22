# Dependency qualification

`compatibility.json` defines the supported substrate and records qualified
dependency versions. `null` means unqualified, never "latest" and never pass.

Run `./entryplug doctor` from the repository root. It records read-only host,
interpreter, command, import, ROS package, and image-conversion evidence under
`runs/doctor/`. The command does not install dependencies or invoke `sudo`.

A green result does not prove camera rendering, trajectory cancellation or hold
behavior, RMW exchange, or SDK round trips. Those checks require separate
runtime evidence.
