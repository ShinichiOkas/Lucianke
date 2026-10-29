// 呼び出し側（S-5 フィクスチャ）
import { Circle, Shape, describe } from "./shapes";

export function totalArea(radii: number[]): number {
  let total = 0;
  for (const r of radii) {
    total += new Circle(r).area();
  }
  return total;
}

export function main(): void {
  const c = new Circle(2.0);
  console.log(describe(c));
  const count: number = "three";
  console.log(totalArea([1.0, 2.0]), count);
}
