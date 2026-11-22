"""Deterministic, deliberately simplified unit economics; no model or network."""

import math

from .errors import ValidationError


_FIELDS = ("users", "revenue_per_user", "variable_cost_per_user", "fixed_cost")


def calculate_unit_economics(inputs: dict) -> dict:
    """Calculate one scenario in consistent caller-supplied units and period.

    Inputs are JSON numbers, never booleans or numeric strings. Results default
    to synthetic. To explicitly mark real inputs, supply synthetic=False and a
    provenance mapping with a nonempty source/locator description for EACH input.
    This records provenance; it does not verify the underlying business facts.
    """
    if not isinstance(inputs, dict):
        raise ValidationError("Economics inputs must be an object")
    values = {}
    for field in _FIELDS:
        value = inputs.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"{field} must be a finite nonnegative number")
        try:
            valid = math.isfinite(value) and value >= 0
        except (OverflowError, ValueError):
            valid = False
        if not valid:
            raise ValidationError(f"{field} must be a finite nonnegative number")
        values[field] = value
    synthetic = inputs.get("synthetic", True)
    if not isinstance(synthetic, bool):
        raise ValidationError("synthetic must be a boolean")
    provenance = inputs.get("provenance")
    if not synthetic and (
        not isinstance(provenance, dict)
        or any(not isinstance(provenance.get(k), str) or not provenance[k].strip()
               for k in _FIELDS)
    ):
        raise ValidationError("Non-synthetic inputs require provenance for each input")

    try:
        margin = values["revenue_per_user"] - values["variable_cost_per_user"]
        contribution = values["users"] * margin
        result = contribution - values["fixed_cost"]
        break_even = values["fixed_cost"] / margin if margin > 0 else None
        finite = all(math.isfinite(v) for v in (margin, contribution, result)
                     if v is not None) and (break_even is None or math.isfinite(break_even))
    except (OverflowError, ZeroDivisionError):
        finite = False
    if not finite:
        raise ValidationError("Calculation overflows; use smaller, consistent units")
    output = {
        "contribution_per_user": margin,
        "contribution_total": contribution,
        "result_after_fixed": result,
        "break_even_users": break_even,
        "synthetic": synthetic,
        "limitations": [
            "Simplified arithmetic, not evidence of demand or business viability.",
            "Use consistent currency and period; taxes, acquisition, capital costs and changing costs are not modelled.",
        ],
    }
    if not synthetic:
        output["provenance"] = {field: provenance[field] for field in _FIELDS}
    return output
