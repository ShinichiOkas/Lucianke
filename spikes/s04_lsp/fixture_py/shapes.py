"""図形の小さなモデル（S-4 フィクスチャ）。"""
import math
import os


class Shape:
    """基底クラス。"""

    def __init__(self, name: str) -> None:
        self.name = name

    def area(self) -> float:
        return 0.0


class Circle(Shape):
    def __init__(self, radius: float) -> None:
        super().__init__("circle")
        self.radius = radius

    def area(self) -> float:
        return math.pi * self.radius**2


def describe(shape: Shape) -> str:
    return f"{shape.name}: {shape.area():.2f}"
