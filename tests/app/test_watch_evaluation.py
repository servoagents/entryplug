import pytest

from entryplug_app.evaluation import evaluate


@pytest.mark.asyncio
async def test_rule_and_assisted_fixture_use_same_finite_sequence(tmp_path):
    report = await evaluate(tmp_path)
    rules, scripted = report["results"]
    assert rules["alerts"] == scripted["alerts"] == 4
    assert rules["model_calls"] == 0
    assert scripted["model_calls"] == 4
    assert rules["timeline"] == scripted["timeline"]
    assert rules["idle_model_calls"] == scripted["idle_model_calls"] == 0
    assert all(result["evidence_ids"] for result in report["results"])
