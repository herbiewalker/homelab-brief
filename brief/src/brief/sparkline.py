"""Trend lines as plain SVG point lists, so a page draws them with no JavaScript and no charting library."""

W, H, PAD = 120.0, 32.0, 2.0


def points(values: list[float], width: float = W, height: float = H) -> tuple[str, str] | None:
    """(polyline points, filled-area points) for a series, or None when there is nothing to draw.

    The line is scaled to its own min and max (a flat series is a flat line in the middle): a trend line
    answers "which way is it going", and the number beside it answers "how much".
    """
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 2:
        return None
    low, high = min(vals), max(vals)
    span = high - low
    step = width / (len(vals) - 1)
    inner = height - 2 * PAD
    xs = [i * step for i in range(len(vals))]
    ys = [height / 2 if span == 0 else PAD + inner * (1 - (v - low) / span) for v in vals]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys, strict=True))
    area = f"0,{height:.1f} {line} {width:.1f},{height:.1f}"
    return line, area


def meter_pct(value: float | None, total: float | None) -> int:
    """A whole percent between 0 and 100 for a meter; 0 when either number is missing."""
    if value is None or not total:
        return 0
    return max(0, min(100, round(100.0 * value / total)))
