from __future__ import annotations

import pytest

from entryplug.visual_task import visual_reach_arguments


@pytest.mark.parametrize("target", [-1, 480, float("nan"), float("inf"), True, "24"])
def test_visual_reach_rejects_invalid_image_rows(target: object) -> None:
    with pytest.raises(ValueError):
        visual_reach_arguments({"target_y_px": target})


def test_visual_reach_accepts_only_one_declared_image_row() -> None:
    assert visual_reach_arguments({"target_y_px": 243}) == {"target_y_px": 243.0}
    with pytest.raises(ValueError):
        visual_reach_arguments({"target_y_px": 243, "joint2": 0.1})
