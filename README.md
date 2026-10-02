# entryplug

```text
      ╭─────╮
──────┤  ◆  ├──────
      ╰──┬──╯
         │
```

**`Entryplug` is an agent embodiment exokernel.**

It is said that *a robot is a network of networks*. Entryplug takes that idea further. An agent does not need to inhabit one chassis. Its body can be distributed across space and machines. Cameras become eyes. Microphones become ears. Remote compute becomes cortex. Actuators become hands. Embedded controllers become reflexes.

Entryplug is a small user space core for giving agents distributed cyber physical presence. It lets them discover resources, acquire a body, validate what that body can actually do and expose those earned capabilities through compact agent interfaces.

It gives agents something close to **root access over embodiment**.

> Intelligence should not be tied to one compact support machine.
> Give it senses. Give it hands. Give it compute. Let it embark into the physical world.

The first mission targets **ROS 2 and `ros2_control`**, **Zenoh**, simulated and real robots, and later **IoT systems over CoAP, MQTT and Matter**, down to embedded nodes running **Zephyr**.

Reasoning stays outside the real time loop. The body executes locally. Entryplug handles embodiment, evidence, composition and change.

**Work in progress.**

The first mission is small. Acquire one physical capability in simulation, prove it, lose part of the body and embody again.


## Use a mission

Entryplug now has an optional resident mission service and a local browser
console. One service owns a workspace; CLI, GUI, and API clients share its runs,
operations, and persistent inbox. The account-free entrance demo uses labelled
**SIMULATED** person events and makes no model calls while waiting.

```bash
entryplug ui
entryplug mission create examples/watch-camera.toml --json
entryplug mission start <mission-id> --json
entryplug demo sequence
entryplug mission status <run-id> --json
```

Python 3.12+ is required. Install a built wheel with `[ui]`, or `[ui,openai]`
for model sign-in. Reinstall an updated 0.0.1 wheel with `--force-reinstall`.
Open **Simulations** to explore a camera recording, rover, or smart room with a
clearly labelled simulated agent. **Add body** offers named simulations and
protocol connection choices. Connections can retry failed bodies, perform
read-only health checks, and show their dependent missions. The browser is an observer: closing it leaves
the mission running. See [installation, mission controls, and systemd](docs/mission-service.md).

## Connect an agent

The console grants an external agent an explicit body/capability scope and a
separate credential. Existing MCP and A2A exports borrow the resident Session;
transport reconnects do not reacquire or close the body. Optional native
inference supports OpenAI API profiles and the official ChatGPT sign-in flow.
Live account/hardware qualification remains opt-in.

See [connections and providers](docs/mission-service.md#connect-a-body-or-external-agent).

## Integrate by API

Use `entryplug_server.client.EntryplugClient` for asynchronous HTTP/SSE access,
or the [OpenAPI contract](docs/api/openapi.json). Commands are idempotent;
revisions, operation outcomes, physical uncertainty, and evidence retain their
own identities. [Python example and event semantics](docs/mission-service.md#integrate-by-api).

[Implementation and measured validation](docs/implementation/mission-delivery.md).
