import json

import pytest

from entryplug_bridges.zenoh import MAX_BYTES, Observations


@pytest.mark.parametrize(
    "keys", [[], ["a", "a"], ["a/*"], ["a/**"], ["/a"], ["a/"], ["a//b"], ["a/../b"]]
)
def test_only_bounded_concrete_keys_are_admitted(keys):
    with pytest.raises(ValueError):
        Observations(keys)


@pytest.mark.parametrize("age", [0, -1, float("nan"), float("inf"), 3601])
def test_receive_age_limit_must_be_finite_and_positive(age):
    with pytest.raises(ValueError):
        Observations(["room/temp"], age)


def test_missing_stale_invalid_deleted_and_closed_never_return_old_values(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("entryplug_bridges.zenoh.time.monotonic", lambda: clock[0])
    cache = Observations(["room/temp"], 5)
    assert cache.read("room/temp")["status"] == "no_sample"
    cache.receive("room/temp", b'{"celsius":21}')
    fresh = cache.read("room/temp")
    assert fresh["value"] == {"celsius": 21} and fresh["source_age_s"] is None
    clock[0] += 6
    assert cache.read("room/temp")["status"] == "stale"
    assert cache.read("room/temp")["value"] is None
    for payload, error in [
        (b"\xff", "unsupported_encoding"),
        (b"a" * (MAX_BYTES + 1), "payload_too_large"),
    ]:
        cache.receive("room/temp", payload)
        assert cache.read("room/temp")["status"] == error
        assert cache.read("room/temp")["value"] is None
    cache.receive("room/temp", b"22")
    cache.receive("room/temp", b"", deleted=True)
    assert cache.read("room/temp")["status"] == "sample_deleted"
    cache.close()
    cache.receive("room/temp", b"23")
    assert cache.read("room/temp")["status"] == "source_closed"
    assert cache.read("room/temp")["value"] is None


def test_text_and_json_are_data_and_unconfigured_keys_are_not_retained():
    cache = Observations(["room/temp"])
    cache.receive("private/secret", b"secret")
    assert not cache.samples
    with pytest.raises(ValueError):
        cache.read("private/secret")
    for payload in [b"hello", b"NaN", b'{"value":Infinity}']:
        cache.receive("room/temp", payload)
        data = cache.read("room/temp")
        assert data["value"] == payload.decode() and data["encoding"] == "text"
        json.dumps(data, allow_nan=False)


def test_bridge_missing_dependencies_explain_the_right_extra(monkeypatch, capsys):
    import sys

    from entryplug.cli import main

    monkeypatch.setitem(sys.modules, "zenoh", None)
    assert main(["bridge", "zenoh", "--connect", "tcp/127.0.0.1:7447", "--key", "room/temp"]) == 2
    assert "entryplug[ui,mcp,zenoh]" in capsys.readouterr().err
