"""もう 1 つの呼び出し側（S-4 フィクスチャ）。"""
from app import total_area
from shapes import Circle, describe


def report() -> str:
    lines = [describe(Circle(r)) for r in (1.0, 3.0)]
    lines.append(f"total={total_area([1.0, 3.0])}")
    return "\n".join(lines)
