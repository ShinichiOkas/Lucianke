import type { KyOptions, Options, Opts, Shape } from './types';
import type { Retry } from './retry';

export function readAll(r: Opts['retry'], xs: Opts[], ro: readonly Retry[], leafy: Opts['nested']['inner'], k: KyOptions, o: Options, s: Shape, u: Opts['count']): number {
  let n = 0;
  n += r.limit;
  n += r.delay(1);
  n += xs.length;
  xs.map((x) => x);
  n += ro.length;
  n += leafy.leaf ? 1 : 0;
  n += k.timeout;
  n += o.timeout;
  n += o.hooks.before.length;
  n += o.inline.before.length;
  o.inline.after();
  n += k.retry === 1 ? 1 : 0;
  n += s.a;
  n += s.kind.length;
  n += u.toFixed().length;
  return n;
}

export async function others<T extends Opts>(p: Promise<Retry>, idx: Opts[keyof Opts], gone: Opts['missing'], gen: T['retry']): Promise<number> {
  const got = await p;
  return got.limit + idx.limit + gone.limit + gen.limit;
}
