"""The lighting spike passes only with a rendered visibility transition."""

from __future__ import annotations

import lamp_spike


def _frames(intensity: float, marker: object) -> list[dict[str, object]]:
    return [
        {"mean_intensity": intensity, "marker": marker},
        {"mean_intensity": intensity, "marker": marker},
    ]


def test_panel_marker_becomes_visible() -> None:
    passed, reason, delta = lamp_spike.evaluate_visibility(
        _frames(5.0, None), _frames(26.0, {"area_px": 720})
    )
    assert passed is True
    assert reason == "MARKER_VISIBILITY_CHANGED"
    assert delta == 21.0


def test_brightness_without_marker_is_not_success() -> None:
    passed, reason, _ = lamp_spike.evaluate_visibility(
        _frames(5.0, None), _frames(26.0, None)
    )
    assert passed is False
    assert reason == "MARKER_NOT_VISIBLE_WHILE_BRIGHT"


def test_marker_visible_in_dark_is_not_success() -> None:
    passed, reason, _ = lamp_spike.evaluate_visibility(
        _frames(5.0, {"area_px": 720}), _frames(26.0, {"area_px": 720})
    )
    assert passed is False
    assert reason == "MARKER_VISIBLE_WHILE_DARK"


def test_small_rgb_change_is_not_success() -> None:
    passed, reason, _ = lamp_spike.evaluate_visibility(
        _frames(5.0, None), _frames(8.0, {"area_px": 720})
    )
    assert passed is False
    assert reason == "INSUFFICIENT_RGB_CHANGE"
