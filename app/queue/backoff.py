import random


def retry_delay(attempt: int, base: float, cap: float, rng: random.Random | None = None) -> float:
    """Seconds to wait before the next try after `attempt` failed (attempt 1 is the first run).

    The delay doubles each attempt up to `cap`, then a random half is kept so that
    many failing jobs do not all retry at the same moment.
    """
    if attempt < 1:
        raise ValueError("attempt starts at 1")
    ceiling = min(cap, base * 2.0 ** (attempt - 1))
    pick = (rng.uniform if rng else random.uniform)(0, ceiling / 2)
    return ceiling / 2 + pick
