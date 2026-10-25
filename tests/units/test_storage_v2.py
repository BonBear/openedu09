"""Calibration file versions 1 and 2 (spec 1.4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from edu09 import storage
from edu09.errors import CalibrationError
from edu09.transport.sim import UNIT1
from edu09.units import Calibration


def _doc(p: Path) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    return out


@pytest.mark.parametrize(
    "cal",
    [Calibration(), Calibration.typical(), Calibration(range_gain={0.5: 0.97}, source="measured")],
)
def test_save_writes_v1_without_v2_fields(tmp_path: Path, cal: Calibration) -> None:
    p = storage.save_calibration(cal, tmp_path / "c.json")
    doc = _doc(p)
    assert doc["version"] == 1 == storage.calibration_file_version(cal)
    assert not {"range_gain_neg", "neg_nonlinearity", "meta"} & set(doc["calibration"])
    assert storage.load_calibration(p) == cal


@pytest.mark.parametrize(
    "cal",
    [
        Calibration(range_gain_neg={0.5: 0.97}),
        Calibration(neg_nonlinearity={0.5: [[4.0, 0.0], [5.0, 0.1]]}),
        Calibration(meta={"tool": "edu09-calibrate"}),
        UNIT1,
    ],
)
def test_save_writes_v2_with_v2_fields(tmp_path: Path, cal: Calibration) -> None:
    p = storage.save_calibration(cal, tmp_path / "c.json")
    assert _doc(p)["version"] == 2
    back = storage.load_calibration(p)
    assert back == cal and back.meta == cal.meta
    assert back.calibration_id == cal.calibration_id


def test_load_accepts_v1_and_v2_and_ignores_number_for_schema(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    body = UNIT1.to_dict()
    for version in (1, 2):  # the parser is the union schema; the number does not matter
        p.write_text(json.dumps({"version": version, "calibration": body}), encoding="utf-8")
        assert storage.load_calibration(p) == UNIT1
    for bad in (0, 3, "2", None, True):
        p.write_text(json.dumps({"version": bad, "calibration": body}), encoding="utf-8")
        with pytest.raises(CalibrationError, match="version"):
            storage.load_calibration(p)


def test_legacy_opaque_nonlinearity_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    legacy = {"version": 1, "points": {"0.5": [[-4.0, 1.06]]}}
    doc = {"version": 1, "calibration": {"base_ofs": 134, "neg_nonlinearity": legacy}}
    p.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(CalibrationError, match="neg_nonlinearity"):
        storage.load_calibration(p)


def test_unknown_key_rejected_in_v2(tmp_path: Path) -> None:
    p = tmp_path / "c.json"
    doc = {"version": 2, "calibration": {**UNIT1.to_dict(), "pos_nonlinearity": {}}}
    p.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(CalibrationError, match="unknown"):
        storage.load_calibration(p)


def test_user_v1_file_loads_unchanged_and_backup(tmp_path: Path) -> None:
    p = tmp_path / "calibration.json"
    user = {
        "calibration": {
            "adc_inverted": False,
            "base_ofs": 134,
            "counts_per_div": 32.0,
            "gnd_fit": {"b": -9.780133333333234, "k": 1.0285999999999993},
            "range_corr": {"0.1": -1, "1": -1},
            "source": "measured",
        },
        "version": 1,
    }
    p.write_text(json.dumps(user, indent=2), encoding="utf-8")
    cal = storage.load_calibration(p)
    assert cal.calibration_id == "measured:c9c4b95f56"
    assert all(cal.range_gain_for(v) == 1.0 == cal.range_gain_neg_for(v) for v in (0.1, 5))
    backup = storage.backup_calibration(p)
    assert backup is not None and json.loads(backup.read_text(encoding="utf-8")) == user
    new = cal.with_gains(pos={0.5: 0.974}, neg={0.5: 0.97}, meta={"tool": "edu09-calibrate"})
    storage.save_calibration(new, p)
    assert _doc(p)["version"] == 2
    assert storage.load_calibration(p).range_gain_neg == {0.5: 0.97}
