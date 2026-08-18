from scipy.stats import norm
from statsmodels.stats.proportion import proportions_ztest


def run_ab_test(
    control_conversions: int,
    control_total: int,
    treatment_conversions: int,
    treatment_total: int,
    alpha: float = 0.05,
    alternative: str = "larger",
) -> dict:
    """
    Run a two-proportion z-test for an A/B test.

    alternative="larger" means:
    H1: treatment conversion rate > control conversion rate
    """

    control_rate = control_conversions / control_total
    treatment_rate = treatment_conversions / treatment_total

    absolute_lift = treatment_rate - control_rate
    relative_lift = absolute_lift / control_rate

    # For proportions_ztest, order matters:
    # count = [treatment successes, control successes]
    # nobs = [treatment total, control total]
    count = [treatment_conversions, control_conversions]
    nobs = [treatment_total, control_total]

    z_stat, p_value = proportions_ztest(
        count=count,
        nobs=nobs,
        alternative=alternative,
    )

    # 95% confidence interval for treatment - control
    standard_error = (
        (treatment_rate * (1 - treatment_rate) / treatment_total)
        + (control_rate * (1 - control_rate) / control_total)
    ) ** 0.5

    z_critical = norm.ppf(1 - alpha / 2)

    ci_lower = absolute_lift - z_critical * standard_error
    ci_upper = absolute_lift + z_critical * standard_error

    statistically_significant = p_value < alpha

    if statistically_significant and absolute_lift > 0:
        recommendation = (
            "The treatment group shows a statistically significant improvement. "
            "The business may consider launching the new version, while monitoring guardrail metrics."
        )
    else:
        recommendation = (
            "The treatment group does not show sufficient statistical evidence of improvement. "
            "The business should not launch the new version based on this test alone."
        )

    return {
        "control_conversion_rate": control_rate,
        "treatment_conversion_rate": treatment_rate,
        "absolute_lift": absolute_lift,
        "relative_lift": relative_lift,
        "z_statistic": z_stat,
        "p_value": p_value,
        "confidence_interval_95": (ci_lower, ci_upper),
        "statistically_significant": statistically_significant,
        "recommendation": recommendation,
    }


if __name__ == "__main__":
    # Example A/B test numbers
    # You can replace these later with real experiment data.
    result = run_ab_test(
        control_conversions=1200,
        control_total=10000,
        treatment_conversions=1250,
        treatment_total=10000,
    )

    print("A/B Test Tool test successful.\n")

    for key, value in result.items():
        if isinstance(value, float):
            print(f"{key}: {value:.4f}")
        else:
            print(f"{key}: {value}")