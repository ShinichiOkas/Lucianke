import { meter } from "./meter";

export function poke(m: any): number {
  m.hits += 1;
  meter.reset();
  return m.hits;
}
