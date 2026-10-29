// 図形の小さなモデル（S-5 フィクスチャ）
export class Shape {
  constructor(public name: string) {}

  area(): number {
    return 0;
  }
}

export class Circle extends Shape {
  constructor(public radius: number) {
    super("circle");
  }

  area(): number {
    return Math.PI * this.radius ** 2;
  }
}

export function describe(shape: Shape): string {
  return `${shape.name}: ${shape.area().toFixed(2)}`;
}
