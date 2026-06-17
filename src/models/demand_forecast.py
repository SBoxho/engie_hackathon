"""Demand forecasting boundary for the next milestone."""


def persistence_forecast(last_value: float, horizon_steps: int) -> list[float]:
    if horizon_steps < 1:
        raise ValueError("horizon_steps must be positive")
    return [float(last_value)] * horizon_steps

