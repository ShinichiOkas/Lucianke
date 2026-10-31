import { Calc, add, deep, deepGo, fl, flush, loop, mainFn, mathDefault, ownAlias, plus, secret } from './lib';
import type { Options } from './lib';
import * as lib from './lib';
import { deepGo as viaDir } from './lib/deep';

export function run(o: Options): number {
  flush();
  fl();
  add(1, 2);
  deep.deepGo();
  deepGo();
  viaDir();
  loop();
  new Calc().run();
  lib.flush();
  plus(1, 2);
  ownAlias();
  mainFn();
  mathDefault();
  secret();
  return o.n;
}
