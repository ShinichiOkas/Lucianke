"""全角・絵文字・結合文字を含む行の位置単位を調べるためのフィクスチャ（S-11）。"""


def target_fn(x: int) -> int:
    return x + 1


# 絵文字（サロゲートペア）・全角・結合文字を、呼び出し対象より前に置く
label = "😀😀😀日本語"; r1 = target_fn(1)
s2 = "全角ＡＢＣ" + str(target_fn(2))
名前 = target_fn(3)
s3 = "é😀"; r3 = target_fn(5)
lint_line = "😀日本語" + undefined_name_zz
