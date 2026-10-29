"""ruff 用: import の並びが乱れていて、未使用 import がある（S-4 フィクスチャ）。"""
import sys
import os
from typing import List
import json


def f(items: List[int]) -> int:
    print(os.sep)
    return len(items) + len(json.dumps(items))
