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
