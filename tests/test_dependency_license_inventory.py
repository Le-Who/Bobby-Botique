"""Tests for truthful, policy-aware production license inventory."""

from email import message_from_string

from scripts.dependency_license_inventory import build_inventory


def _metadata(**headers: str):
    return message_from_string("\n".join(f"{name}: {value}" for name, value in headers.items()))


def test_license_inventory_records_spdx_classifiers_and_unknowns() -> None:
    components = [{"name": "alpha", "version": "1.0"}, {"name": "beta", "version": "2.0"}]
    metadata = {
        "alpha": _metadata(**{"License-Expression": "MIT", "Classifier": "License :: OSI Approved :: MIT License"}),
        "beta": _metadata(),
    }

    report = build_inventory(components, metadata.__getitem__, denylist=())

    assert report["unknown_count"] == 1
    assert report["violations"] == []
    assert report["packages"][0] == {
        "name": "alpha",
        "version": "1.0",
        "license_expression": "MIT",
        "license": None,
        "classifiers": ["License :: OSI Approved :: MIT License"],
        "status": "known",
    }
    assert report["packages"][1]["status"] == "unknown"


def test_license_inventory_fails_only_on_an_explicit_denylist_match() -> None:
    components = [{"name": "copyleft", "version": "3.0"}]
    metadata = {"copyleft": _metadata(**{"License-Expression": "GPL-3.0-only OR MIT"})}

    report = build_inventory(components, metadata.__getitem__, denylist=("GPL-3.0-only",))

    assert report["violations"] == [{"name": "copyleft", "version": "3.0", "denied": ["GPL-3.0-only"]}]
