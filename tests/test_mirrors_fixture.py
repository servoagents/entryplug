from __future__ import annotations

import ast
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCENE = ROOT / "containers" / "harbor" / "mirrors_scene.xml"
MIRRORS = ROOT / "containers" / "harbor" / "mirrors.py"
SMOKE = ROOT / "containers" / "harbor" / "smoke.sh"


def _scene() -> ET.Element:
    return ET.parse(SCENE).getroot()


def test_mirrors_scene_has_two_candidate_cameras_on_separate_mechanisms() -> None:
    scene = _scene()
    cameras = {camera.attrib["name"]: camera for camera in scene.findall(".//camera")}

    assert set(cameras) == {"camera", "mirror_camera", "spectator_camera"}
    assert cameras["camera"].attrib["resolution"] == "640 480"
    assert cameras["mirror_camera"].attrib["resolution"] == "640 480"

    controlled_parent = scene.find(".//body[@name='forearm']/camera[@name='camera']/..")
    independent_parent = scene.find(
        ".//body[@name='mirror_forearm']/camera[@name='mirror_camera']/.."
    )
    assert controlled_parent is not None
    assert independent_parent is not None
    assert controlled_parent is not independent_parent


def test_spectator_camera_is_world_fixed_observer_only() -> None:
    scene = _scene()
    spectator = scene.find("./worldbody/camera[@name='spectator_camera']")

    assert spectator is not None
    assert spectator.attrib["mode"] == "fixed"
    assert spectator.attrib["resolution"] == "640 480"


def test_association_node_cannot_subscribe_to_spectator_camera() -> None:
    tree = ast.parse(MIRRORS.read_text(encoding="utf-8"))
    observer = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "MirrorsObserver"
    )
    string_constants = {
        node.value
        for node in ast.walk(observer)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert "/camera/mirror/image_raw" in string_constants
    assert "/camera/spectator/image_raw" not in string_constants


def test_spectator_capture_runs_only_after_association_process() -> None:
    script = SMOKE.read_text(encoding="utf-8")

    association = script.index("containers/harbor/mirrors.py")
    spectator = script.index("containers/harbor/spectator_capture.py")
    report = script.index("containers/harbor/mirrors_report.py")
    assert association < spectator < report


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
