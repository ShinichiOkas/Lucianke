import { Base, Pingable } from "./base";

export class Sub extends Base {
  run(): number {
    return this.helper();
  }
}

export interface Loud extends Pingable {
  shout(): void;
}

export function use(s: Sub, l: Loud): number {
  l.ping();
  return s.helper();
}

export function fresh(): number {
  return new Sub().helper();
}
