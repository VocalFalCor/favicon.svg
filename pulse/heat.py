from __future__ import annotations


def heat_score(recent_listens: int, local_votes: int, days_since_signal: float) -> float:
    """Return a 0–100 score: 40% listens, 30% local votes, 30% freshness."""
    freshness = max(0.0, 1.0 - max(0.0, days_since_signal) / 30.0)
    score = (
        40.0 * min(max(recent_listens, 0) / 50.0, 1.0)
        + 30.0 * min(max(local_votes, 0) / 10.0, 1.0)
        + 30.0 * freshness
    )
    return round(score, 1)


def heat_visuals(score: float) -> dict[str, float | str]:
    """Map a HeatScore to SVG node size, glow color, and pulse period."""
    score = min(max(score, 0.0), 100.0)
    stops = (
        (0.0, (53, 214, 255)),
        (0.5, (123, 97, 255)),
        (1.0, (255, 56, 160)),
    )
    position = score / 100.0
    for (start, first), (end, second) in zip(stops, stops[1:]):
        if position <= end:
            ratio = (position - start) / (end - start)
            channels = tuple(round(a + (b - a) * ratio) for a, b in zip(first, second))
            color = "#" + "".join(f"{channel:02X}" for channel in channels)
            break
    else:
        color = "#FF38A0"

    return {
        "node_radius": round(12.0 + score * 0.36, 1),
        "color": color,
        "glow": round(8.0 + score * 0.24, 1),
        "pulse_seconds": round(4.5 - score * 0.034, 2),
    }
