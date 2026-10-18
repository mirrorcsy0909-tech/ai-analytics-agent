"""Validated independent-binomial comparison with explicit sparse-data handling."""

import math
from numbers import Integral, Real

from scipy.stats import fisher_exact, norm
from statsmodels.stats.proportion import confint_proportions_2indep, proportions_ztest


def run_ab_test(control_conversions: int, control_total: int,
                treatment_conversions: int, treatment_total: int,
                alpha: float = 0.05, alternative: str = "larger") -> dict:
    """Test independent counts; 'larger' means treatment > control.

    Use pooled z for non-sparse tables and Fisher exact if any observed
    success/failure cell is below five. Intervals are separately two-sided.
    This does not establish randomization, independence or practical benefit.
    """
    counts = [control_conversions, control_total, treatment_conversions, treatment_total]
    if any(isinstance(v, bool) or not isinstance(v, Integral) for v in counts):
        raise ValueError("Conversion counts and group totals must be integers.")
    if control_total <= 0 or treatment_total <= 0:
        raise ValueError("Both group totals must be positive.")
    if not 0 <= control_conversions <= control_total or not 0 <= treatment_conversions <= treatment_total:
        raise ValueError("Each conversion count must be between zero and its group total.")
    if isinstance(alpha, bool) or not isinstance(alpha, Real) or not math.isfinite(alpha) or not 0 < alpha < 1:
        raise ValueError("alpha must be finite and strictly between zero and one.")
    if alternative not in {"larger", "smaller", "two-sided"}:
        raise ValueError("alternative must be larger, smaller, or two-sided.")
    control_rate, treatment_rate = control_conversions / control_total, treatment_conversions / treatment_total
    absolute_lift = treatment_rate - control_rate
    relative_lift = absolute_lift / control_rate if control_rate else None
    cells = [treatment_conversions, treatment_total - treatment_conversions,
             control_conversions, control_total - control_conversions]
    pooled = (control_conversions + treatment_conversions) / (control_total + treatment_total)
    expected = [treatment_total * pooled, treatment_total * (1 - pooled),
                control_total * pooled, control_total * (1 - pooled)]
    sparse = min([*cells, *expected]) < 5
    warnings = ["Assumes independent binomial outcomes; randomization and sample-ratio mismatch were not verified."]
    if sparse:
        direction = {"larger": "greater", "smaller": "less", "two-sided": "two-sided"}[alternative]
        p_value = float(fisher_exact([cells[:2], cells[2:]], alternative=direction).pvalue)
        z_stat = None
        ci_lower, ci_upper = confint_proportions_2indep(
            treatment_conversions, treatment_total, control_conversions, control_total,
            method="newcomb", compare="diff", alpha=alpha)
        test_method, ci_method = "fisher_exact", "newcombe_wilson"
        warnings.append("Sparse observed or pooled expected counts: used Fisher exact instead of the z approximation.")
    else:
        z_stat, p_value = proportions_ztest(
            [treatment_conversions, control_conversions], [treatment_total, control_total], alternative=alternative)
        se = math.sqrt(treatment_rate * (1 - treatment_rate) / treatment_total
                       + control_rate * (1 - control_rate) / control_total)
        critical = norm.ppf(1 - alpha / 2)
        ci_lower, ci_upper = absolute_lift - critical * se, absolute_lift + critical * se
        test_method, ci_method = "two_proportion_z", "wald"
    significant = bool(p_value < alpha)
    conclusion = ("improved" if absolute_lift > 0 else "declined") if significant else "not_significant"
    recommendation = {
        "improved": "Evidence supports higher treatment conversion for the specified test. Check practical benefit and experiment validity before rollout.",
        "declined": "Evidence supports lower treatment conversion for the specified test; do not interpret this as an improvement.",
        "not_significant": "The specified test does not provide sufficient evidence of a conversion difference in its tested direction."
    }[conclusion]
    result = {"control_conversion_rate": control_rate, "treatment_conversion_rate": treatment_rate,
              "absolute_lift": absolute_lift, "relative_lift": relative_lift,
              "z_statistic": None if z_stat is None else float(z_stat), "p_value": float(p_value),
              "confidence_interval": (float(ci_lower), float(ci_upper)),
              "confidence_level": 1 - alpha, "confidence_interval_method": ci_method,
              "confidence_interval_sidedness": "two-sided", "test_method": test_method,
              "alternative": alternative, "alpha": float(alpha), "statistically_significant": significant,
              "conclusion": conclusion, "recommendation": recommendation, "warnings": warnings}
    if math.isclose(alpha, 0.05, abs_tol=1e-12):
        result["confidence_interval_95"] = result["confidence_interval"]
    return result
