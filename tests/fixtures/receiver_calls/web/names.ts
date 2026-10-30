import * as namesMod from "./names_mod";

export class Ledger {
  persist(): number {
    return 1;
  }
}

export function untyped(x) {
  return x.persist();
}

export function annotated(x: Ledger) {
  return x.persist();
}

export function built() {
  const x = new Ledger();
  return x.persist();
}

export function moduleFunction() {
  return namesMod.persist();
}
