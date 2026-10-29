"""The only bridge to the installed, versioned property predictors.

The private namespace loads just model modules, bypassing steel_forest's eager
package initializer and its campaign, forest, and review implementations.
"""
import atexit
import importlib
import json
import math
import subprocess
from pathlib import Path
import sys
import types

SOURCE = Path(__file__).resolve().parents[2] / "steel_forest/src/steel_forest"
_NAMESPACE = "_steel_hf_physics"
_CORROSION = None

_TOOL_METRICS = {
    "material_candidate_evaluation": ["cumulative_corrosion_loss_um", "relative_corrosion_vs_q345b_gold",
                                      "saving_vs_3ni_yuan_per_t", "yield_strength_mpa",
                                      "tensile_strength_mpa", "elongation_pct"],
    "property-lgbm-v1-remanence": ["remanence"],
    "property-lgbm-v1-relative-permeability": ["relative_permeability"],
    "nms-plate-mechanical": ["yield_strength", "tensile_strength", "elongation", "yield_to_tensile_ratio", "pren"],
    "hea-reported-density-nist38-lightgbm": ["reported_density"],
    "hea-reported-yield-strength-lightgbm": ["reported_yield_strength"],
    "hea-reported-tensile-strength-lightgbm": ["reported_tensile_strength"],
    "hea-reported-elongation-lightgbm": ["reported_elongation"],
}


class PredictionError(RuntimeError):
    """One inference failed; its missing result must not become evidence."""


def _predict(function, *args, **kwargs):
    """Translate known execution failures; native asset/contract errors stay fatal."""
    try:
        return function(*args, **kwargs)
    except (ValueError, RuntimeError, OSError, ArithmeticError, subprocess.TimeoutExpired) as exc:
        cause = exc.__cause__ or exc
        message = str(cause)
        execution_errors = ('TimeoutError:', 'FloatingPointError:', 'OverflowError:',
                            'corrosion model worker timed out', 'plate predictor exited before response',
                            'HEA runtime exited without a response', 'non-finite model prediction',
                            'negative cumulative corrosion loss', 'predictor returned a non-finite value')
        if (isinstance(cause, (TimeoutError, BrokenPipeError, ArithmeticError, subprocess.TimeoutExpired))
                or any(error in message for error in execution_errors)):
            raise PredictionError(str(exc)) from exc
        raise


def select_tools(scene, tool_ids=None):
    """Keep only requested registered tools, in registration order."""
    registered = {tool["id"]: tool for tool in scene["tools"]}
    if tool_ids is None:
        return list(registered.values())
    if not isinstance(tool_ids, (list, tuple)) or not tool_ids or not all(isinstance(v, str) for v in tool_ids):
        raise ValueError("tools must be a nonempty list of registered tool IDs")
    unknown = set(tool_ids) - registered.keys()
    if unknown:
        raise ValueError(f"Unknown tools for {scene['id']}: {sorted(unknown)}")
    return [tool for key, tool in registered.items() if key in tool_ids]


def tool_metrics(scene, tool_ids=None):
    """Report registered scene metric keys produced by the selected tools."""
    produced = {key for tool in select_tools(scene, tool_ids) for key in _TOOL_METRICS[tool["id"]]}
    return [metric["key"] for metric in scene["metrics"] if metric["key"] in produced]


def _module(name):
    if _NAMESPACE not in sys.modules:
        package = types.ModuleType(_NAMESPACE)
        package.__path__ = [str(SOURCE)]
        sys.modules[_NAMESPACE] = package
    return importlib.import_module(f"{_NAMESPACE}.{name}")


def validate_protocol(scene, protocol, *, tool_ids=None):
    """Check actual model input fields before a campaign; no predictor is loaded.

    notes and manufacturing_state are context, not predictor features.
    """
    ids = {tool["id"] for tool in select_tools(scene, tool_ids)}
    if scene["short_name"] in ("corrosion", "nonmagnetic"):
        allowed = set(scene["protocol"]) | {"notes"}
        if scene["short_name"] == "nonmagnetic":
            allowed |= {"mechanical_conditions", "manufacturing_state", "mechanical_condition_policy"}
        unknown = set(protocol) - allowed
        if unknown:
            raise ValueError(f"Unsupported protocol fields for {scene['short_name']}: {sorted(unknown)}")
    if scene["short_name"] == "nonmagnetic" and "nms-plate-mechanical" in ids:
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
        if ids == {"hea-reported-density-nist38-lightgbm"}:
            if "process_text" in protocol and not isinstance(protocol["process_text"], str):
                raise ValueError("Excluded process_text context must be text")
            if "testing" in protocol and not isinstance(protocol["testing"], dict):
                raise ValueError("Excluded testing context must be an object")
            return
        testing = protocol.get("testing")
        temperature = testing.get("test_temperature_C") if isinstance(testing, dict) else None
        if not isinstance(temperature, (int, float)) or isinstance(temperature, bool) or not 0 <= temperature <= 700:
            raise ValueError("HEA requires protocol.testing.test_temperature_C in 0–700 °C")
        if not isinstance(protocol.get("process_text"), str) or not protocol["process_text"].strip():
            raise ValueError("HEA requires process_text; state unknown conditions explicitly")


def _save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def evaluate(scene, composition, protocol, workdir, *, tool_ids=None):
    """Return normalized metrics without changing the registered model calculations."""
    global _CORROSION
    workdir.mkdir(parents=True, exist_ok=True)
    raw, metrics, limitations = {}, {}, list(scene["limitations"])
    selected = select_tools(scene, tool_ids)
    versions = {tool["id"]: tool["version"] for tool in selected}
    if scene["short_name"] == "corrosion":
        module = _module("material_tools")
        if versions.get(module.TOOL_ID) != module.TOOL_VERSION:
            raise ValueError("The registered corrosion tool version differs from its installed predictor")
        if _CORROSION is None:
            _CORROSION = module.MaterialModelRuntime()
            atexit.register(_CORROSION.close)
        raw["corrosion"] = _predict(_CORROSION.predict, {
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
        targets = {
            "property-lgbm-v1-remanence": ("remanence", "剩磁"),
            "property-lgbm-v1-relative-permeability": ("relative_permeability", "相对磁导率"),
            "nms-plate-mechanical": ("yield_strength", "屈服强度"),
        }
        requested = [targets[tool["id"]] for tool in selected]
        actual = [module.nonmagnetic_tool_spec(target) for target, _ in requested]
        if {tool.tool_id: tool.version for tool in actual} != versions:
            raise ValueError("The registered nonmagnetic tool versions differ from the installed predictors")
        raw["nonmagnetic"] = _predict(module.evaluate_nonmagnetic_candidate,
            composition, protocol, targets=[native for _, native in requested])
        metrics.update(raw["nonmagnetic"]["metrics"])
        if any(target != "yield_strength" for target, _ in requested):
            limitations.append("磁性预测仅使用成分；工艺和测试条件不进入磁性模型，不能据此判断条件变化的磁性效应。")
    else:
        payload = {"composition_wt_pct": composition, "composition_unit": "wt_pct",
                   "composition_complete": True, "protocol": protocol}
        functions = {
            "hea-reported-density-nist38-lightgbm": ("density", "hea_density_tools", "evaluate_hea_density", None),
            "hea-reported-yield-strength-lightgbm": ("yield", "hea_strength_tools", "evaluate_hea_yield", "YS"),
            "hea-reported-tensile-strength-lightgbm": ("tensile", "hea_strength_tools", "evaluate_hea_tensile", "UTS"),
            "hea-reported-elongation-lightgbm": ("elongation", "hea_tools", "evaluate_hea_candidate", None),
        }
        for tool in selected:
            name, module_name, function_name, target = functions[tool["id"]]
            module = _module(module_name)
            actual = module.binding(target) if target else module.binding()
            if (actual["tool_id"], actual["version"]) != (tool["id"], tool["version"]):
                raise ValueError("The registered HEA tool version differs from its installed predictor")
            raw[name] = _predict(getattr(module, function_name), payload)
            metrics.update(raw[name]["metrics"])
            limitations.extend(raw[name].get("warnings", []))
            _save(workdir / f"{name}.json", raw[name])
    for name, result in raw.items():
        if scene["short_name"] != "hea":
            _save(workdir / f"{name}.json", result)
    result = {"metrics": metrics, "tools": selected,
              "limitations": list(dict.fromkeys(limitations)), "qualification": "model_diagnostic"}
    _save(workdir / "prediction.json", result)
    return result
