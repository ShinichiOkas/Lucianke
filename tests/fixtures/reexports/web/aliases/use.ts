import type { Bag, Box, Chain, Derived, Either, EngineAlias, Fn, Loose, MaybeBase, Options } from './types';

export function readAll(o: Options, b: Box<number>, e: Either, m: MaybeBase, g: EngineAlias, l: Loose, f: Fn, c: Chain, d: Derived, bag: Bag): number {
  let n = 0;
  n += o.retry;
  n += o.extra.length;
  o.tick();
  n += b.retry;
  n += e.onlyOther ? 1 : 0;
  n += e.shared ? 1 : 0;
  n += m ? m.retry : 0;
  n += g.start();
  n += l.retry ?? 0;
  n += f.length;
  n += f.arity;
  n += c.retry;
  n += d.retry;
  n += d.own;
  n += bag.anything;
  return n;
}
