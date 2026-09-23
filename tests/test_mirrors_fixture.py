from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCENE = ROOT / "containers" / "harbor" / "mirrors_scene.xml"


def _scene() -> ET.Element:
    return ET.parse(SCENE).getroot()


def test_mirrors_scene_has_two_rendered_cameras_on_separate_mechanisms() -> None:
    scene = _scene()
    cameras = {camera.attrib["name"]: camera for camera in scene.findall(".//camera")}

    assert set(cameras) == {"camera", "mirror_camera"}
    assert cameras["camera"].attrib["resolution"] == "640 480"
    assert cameras["mirror_camera"].attrib["resolution"] == "640 480"

    controlled_parent = scene.find(".//body[@name='forearm']/camera[@name='camera']/..")
    independent_parent = scene.find(
        ".//body[@name='mirror_forearm']/camera[@name='mirror_camera']/.."
    )
    assert controlled_parent is not None
    assert independent_parent is not None
    assert controlled_parent is not independent_parent


def test_mirrors_scene_keeps_floor_below_the_probe_motion() -> None:
    floor = _scene().find(".//geom[@name='floor']")

    assert floor is not None
    floor_position = [float(value) for value in floor.attrib["pos"].split()]
    assert floor_position[2] <= -2.5
    assert floor.attrib["conaffinity"] == "0"


def test_mirrors_scene_has_independent_actuators() -> None:
    scene = _scene()
    actuators = {
        actuator.attrib["name"]: actuator.attrib["joint"]
        for actuator in scene.findall("./actuator/position")
    }

    assert actuators == {
        "joint1": "joint1",
        "joint2": "joint2",
        "mirror_joint1": "mirror_joint1",
        "mirror_joint2": "mirror_joint2",
    }
