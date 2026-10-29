// 全角・絵文字・結合文字を含む行の位置単位を調べるためのフィクスチャ（S-11）
export function targetFn(x: number): number {
  return x + 1;
}

// 絵文字（サロゲートペア）・全角・結合文字を、呼び出し対象より前に置く
export const label = "😀😀😀日本語"; export const r1 = targetFn(1);
export const s2 = "全角ＡＢＣ" + String(targetFn(2));
export const 名前 = targetFn(3);
export const s3 = "é😀"; export const r3 = targetFn(5);
export const lintLine = "😀日本語" + undefinedNameZz;
