"""
cutmath.py - the two small functions the Cut page needs to preview cut points while the user types.

The authoritative implementation is in dtv_tools.py (parse_time_input / idr_candidates); the GUI never imports that script,
so these are copies. tests/test_gui_contract.py checks that they give the same answers.
"""
import bisect


def parse_time(text) -> float:
    """'HH:MM:SS.mmm', 'MM:SS' or 'SS.mmm' (comma accepted) -> seconds. Raises ValueError with a message for the user."""
    try:
        parts = [float(p) for p in str(text).strip().replace(",", ".").split(":")]
    except ValueError:
        raise ValueError(f"Tempo inválido: {text!r} (use HH:MM:SS.mmm, MM:SS ou segundos).") from None
    if not 1 <= len(parts) <= 3 or any(p < 0 for p in parts):
        raise ValueError(f"Tempo inválido: {text!r}.")
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def idr_candidates(idr, target):
    """idr: [(seconds, frame, kind)] sorted -> {"prev", "next", "nearest"}."""
    times = [x[0] for x in idr]
    pos = bisect.bisect_left(times, target)
    prev_i = max(0, pos - 1) if (pos == len(times) or times[pos] > target) else pos
    next_i = min(len(times) - 1, pos)
    if prev_i == next_i and next_i + 1 < len(times):
        next_i = prev_i + 1
    p, nx = idr[prev_i], idr[next_i]
    return {"prev": p, "next": nx, "nearest": p if abs(p[0] - target) <= abs(nx[0] - target) else nx}
