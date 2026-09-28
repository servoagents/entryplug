"""Borrowed Light's camera fixture must remain independent of the robot arm."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).parents[2] / "containers" / "harbor"


def test_panel_has_a_world_fixed_camera_marker_and_named_lamp() -> None:
    scene = ET.parse(ROOT / "panel_scene.xml").getroot()

    camera = scene.find("./worldbody/camera[@name='panel_camera']")
    marker = scene.find("./worldbody/geom[@name='red_marker']")
    lamp = scene.find("./worldbody/light[@name='fixture_lamp']")
    assert camera is not None and camera.attrib["resolution"] == "320 240"
    assert marker is not None and marker.attrib["rgba"].startswith("0.95 0.04 0.04")
    assert lamp is not None
    assert scene.findall(".//joint") == []
    assert scene.find("./actuator") is None


def test_panel_ros_description_has_no_motion_interfaces() -> None:
    robot = ET.parse(ROOT / "panel_robot.urdf").getroot()

    joints = robot.findall("./joint")
    assert len(joints) == 1 and joints[0].attrib["type"] == "fixed"
    assert robot.findall("./ros2_control/joint") == []
    assert robot.find("./ros2_control/sensor[@name='panel_camera']") is not None
