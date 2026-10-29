"""The only bridge to the installed, versioned property predictors.

The private namespace loads just model modules, bypassing steel_forest's eager
package initializer and its campaign, forest, and review implementations.
"""
import atexit
import importlib
import json
import math
from pathlib import Path
import sys
import types

SOURCE = Path(__file__).resolve().parents[2] / "steel_forest/src/steel_forest"
_NAMESPACE = "_steel_hf_physics"
_CORROSION = None


def _module(name):
    if _NAMESPACE not in sys.modules:
        package = types.ModuleType(_NAMESPACE)
        package.__path__ = [str(SOURCE)]
        sys.modules[_NAMESPACE] = package
    return importlib.import_module(f"{_NAMESPACE}.{name}")


def validate_protocol(scene, protocol):
    """Check input fields before a campaign starts; no predictor is loaded."""
    if scene["short_name"] == "nonmagnetic":
        conditions = protocol.get("mechanical_conditions", {})
        if not isinstance(conditions, dict):
            raise ValueError("mechanical_conditions must be an object; use {} for unknown conditions")
        asset = next(t["asset"] for t in scene["tools"] if t["id"] == "nms-plate-mechanical")
        contract = json.loads((SOURCE.parents[2] / asset).with_name("feature_contract.json").read_text())
        derived = set("CN Cr_equiv Ni_equiv Mn_N temp_C_interaction temperature_kelvin_input CN_temperature_interaction cold_N_interaction cold_C_interaction log1p_cold_reduction".split())
        aliases = set("test_temperature_C solution_temperature_C solution_time_h post_heat_temperature_C post_heat_time_h cold_reduction_pct plate_thickness_mm".split())
        categorical = set(contract["categorical"])
        numeric = {k for k in contract["numeric"] if not k.endswith("_wt_pct")} - derived
        for key, value in conditions.items():
            if key not in numeric | categorical | aliases:
                raise ValueError(f"Unsupported mechanical condition: {key}")
            if value is None:
                continue
            if key in categorical:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{key} must be text or null")
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{key} must be a finite number or null")
    elif scene["short_name"] == "hea":
        if set(protocol) - {"process_text", "testing"}:
            raise ValueError("HEA protocol supports process_text and testing only")
        testing = protocol.get("testing")
        temperature = testing.get("test_temperature_C") if isinstance(testing, dict) else None
        if not isinstance(temperature, (int, float)) or isinstance(temperature, bool) or not 0 <= temperature <= 700:
            raise ValueError("HEA requires protocol.testing.test_temperature_C in 0–700 °C")
        if not isinstance(protocol.get("process_text"), str) or not protocol["process_text"].strip():
            raise ValueError("HEA requires process_text; state unknown conditions explicitly")


def _save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def evaluate(scene, composition, protocol, workdir):
    """Return normalized metrics without changing the registered model calculations."""
    global _CORROSION
    workdir.mkdir(parents=True, exist_ok=True)
    raw, metrics, limitations = {}, {}, list(scene["limitations"])
    versions = {tool["id"]: tool["version"] for tool in scene["tools"]}
    if scene["short_name"] == "corrosion":
        module = _module("material_tools")
        if versions.get(module.TOOL_ID) != module.TOOL_VERSION:
            raise ValueError("The registered corrosion tool version differs from its installed predictor")
        if _CORROSION is None:
            _CORROSION = module.MaterialModelRuntime()
            atexit.register(_CORROSION.close)
        raw["corrosion"] = _CORROSION.predict({
            "candidate_id": workdir.name, "composition_wt_pct": composition,
            "composition_complete": True, "protocol": protocol,
        })
        metrics.update(raw["corrosion"]["decision_summary"]["metrics"])
        limitations.extend(raw["corrosion"]["applicability"]["known_limits"])
        mechanical = raw["corrosion"]["mechanical"]
        if mechanical.get("status") != "predicted":
            limitations.append("力学诊断不可用：" + mechanical.get("reason", "见原始工具输出"))
    elif scene["short_name"] == "nonmagnetic":
        module = _module("nonmagnetic_tools")
        actual = [module.nonmagnetic_tool_spec(target) for target in ("remanence", "relative_permeability", "yield_strength")]
        if {tool.tool_id: tool.version for tool in actual} != versions:
            raise ValueError("The registered nonmagnetic tool versions differ from the installed predictors")
        raw["nonmagnetic"] = module.evaluate_nonmagnetic_candidate(composition, protocol)
        metrics.update(raw["nonmagnetic"]["metrics"])
    else:
        payload = {"composition_wt_pct": composition, "composition_unit": "wt_pct",
                   "composition_complete": True, "protocol": protocol}
        functions = [
            ("density", _module("hea_density_tools").evaluate_hea_density),
            ("yield", _module("hea_strength_tools").evaluate_hea_yield),
            ("tensile", _module("hea_strength_tools").evaluate_hea_tensile),
            ("elongation", _module("hea_tools").evaluate_hea_candidate),
        ]
        actual = [_module("hea_density_tools").binding(), _module("hea_tools").binding(),
                  _module("hea_strength_tools").binding("YS"), _module("hea_strength_tools").binding("UTS")]
        if {tool["tool_id"]: tool["version"] for tool in actual} != versions:
            raise ValueError("The registered HEA tool versions differ from the installed predictors")
        for name, function in functions:
            raw[name] = function(payload)
            metrics.update(raw[name]["metrics"])
            limitations.extend(raw[name].get("warnings", []))
            _save(workdir / f"{name}.json", raw[name])
    for name, result in raw.items():
        if scene["short_name"] != "hea":
            _save(workdir / f"{name}.json", result)
    result = {"metrics": metrics, "tools": scene["tools"],
              "limitations": list(dict.fromkeys(limitations)), "qualification": "model_diagnostic"}
    _save(workdir / "prediction.json", result)
    return result
