"""Load a scenario, check an explicit design space, and evaluate one recipe."""
from copy import deepcopy
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAMES = {
    "corrosion": "fixed_wheel_cyclic_immersion",
    "nonmagnetic": "weak_field_nonmagnetic_plate",
    "hea": "high_temperature_high_entropy_alloy",
}


def load(name):
    """Read the single active definition; accept a short name or scenario id."""
    short = next((key for key, value in NAMES.items() if name in (key, value)), None)
    if short is None:
        raise ValueError(f"Unknown scenario {name!r}; choose {', '.join(NAMES)}")
    return json.loads((ROOT / short / "scene.json").read_text())


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_ranges(scenario, ranges):
    """Return a feasible wt.% box. Zero means excluded; missing means unspecified."""
    scene = load(scenario) if isinstance(scenario, str) else scenario
    if not isinstance(ranges, dict) or not ranges:
        raise ValueError("composition_ranges_wt_pct must explicitly describe this campaign")
    result = deepcopy(ranges)
    allowed, balance = set(scene["elements"]), scene.get("balance")
    if set(result) - allowed:
        raise ValueError(f"Unsupported elements: {sorted(set(result) - allowed)}")
    if balance:
        missing = allowed - {balance} - set(result)
        if missing:
            raise ValueError(f"Declare each element range, using [0, 0] when excluded: {sorted(missing)}")
        result.setdefault(balance, [0, 100])
    for element, bounds in result.items():
        if (not isinstance(bounds, (list, tuple)) or len(bounds) != 2
                or not all(_number(v) for v in bounds) or not 0 <= bounds[0] <= bounds[1] <= 100):
            raise ValueError(f"{element}: expected [minimum, maximum] within 0–100 wt.%")
    if math.fsum(v[0] for v in result.values()) > 100 + 1e-8 or math.fsum(v[1] for v in result.values()) < 100 - 1e-8:
        raise ValueError("The declared ranges cannot form a complete 100 wt.% recipe")
    return result


def complete_composition(scenario, composition, ranges=None):
    """Validate a complete recipe; only steel has an explicitly declared Fe balance."""
    scene = load(scenario) if isinstance(scenario, str) else scenario
    if not isinstance(composition, dict) or not composition:
        raise ValueError("composition must be a nonempty wt.% object")
    result = dict(composition)
    allowed, balance = set(scene["elements"]), scene.get("balance")
    if set(result) - allowed:
        raise ValueError(f"Unsupported elements: {sorted(set(result) - allowed)}")
    if balance:
        missing = allowed - {balance} - set(result)
        if missing:
            raise ValueError(f"Missing composition coordinates: {sorted(missing)}")
    if any(not _number(v) or not 0 <= v <= 100 for v in result.values()):
        raise ValueError("Every composition value must be finite and within 0–100 wt.%")
    if balance and balance not in result:
        result[balance] = 100 - math.fsum(result.values())
    if any(v < 0 for v in result.values()) or not math.isclose(math.fsum(result.values()), 100, abs_tol=1e-6):
        raise ValueError("Composition must total 100 wt.%; HEA has no automatic balance element")
    if ranges is not None:
        bounds = validate_ranges(scene, ranges)
        for element in set(result) | set(bounds):
            value = result.get(element, 0)
            low, high = bounds.get(element, [0, 0])
            if not low - 1e-8 <= value <= high + 1e-8:
                raise ValueError(f"{element}={value} lies outside the campaign range [{low}, {high}]")
    return result


def effective_protocol(scenario, protocol):
    """Combine declared conditions with the fixed scenario comparison protocol."""
    scene = load(scenario) if isinstance(scenario, str) else scenario
    if not isinstance(protocol, dict):
        raise ValueError("protocol must be an object; use {} for unspecified optional conditions")
    fixed = scene["protocol"]
    for key, value in fixed.items():
        if key in protocol and protocol[key] != value:
            raise ValueError(f"{key} is fixed by scenario {scene['id']}: {value!r}")
    from .tools import validate_protocol
    result = {**deepcopy(fixed), **deepcopy(protocol)}
    validate_protocol(scene, result)
    return result


def evaluate(scenario, composition, protocol, workdir):
    """Run current property tools; write their raw outputs under this candidate's workdir."""
    from .tools import evaluate as run_tools
    scene = load(scenario) if isinstance(scenario, str) else scenario
    recipe = complete_composition(scene, composition)
    conditions = effective_protocol(scene, protocol)
    return run_tools(scene, recipe, conditions, Path(workdir))
