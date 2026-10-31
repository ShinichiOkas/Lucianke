export function add(a: number, b: number): number {
  return a + b;
}

function secret(): number {
  return 9;
}

export interface Options {
  n: number;
}

export class Calc {
  run(): number {
    return secret();
  }
}

export default function mathDefault(): number {
  return 0;
}
