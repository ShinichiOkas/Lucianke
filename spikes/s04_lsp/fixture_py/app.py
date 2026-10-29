"""呼び出し側（S-4 フィクスチャ）。"""
from shapes import Circle, describe


def total_area(radii: list[float]) -> float:
    total = 0.0
    for r in radii:
        total += Circle(r).area()
    return total


def main() -> None:
    c = Circle(2.0)
    print(describe(c))
    count: int = "three"
    print(total_area([1.0, 2.0]), count)
