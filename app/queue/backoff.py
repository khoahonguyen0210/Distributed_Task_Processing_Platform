import random


def retry_delay(attempt: int, base: float, cap: float, rng: random.Random | None = None) -> float:
    if attempt < 1:
        raise ValueError("attempt starts at 1")
    ceiling = min(cap, base * 2.0 ** (attempt - 1))
    pick = (rng.uniform if rng else random.uniform)(0, ceiling / 2)
    return ceiling / 2 + pick
