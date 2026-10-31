import main, { other } from './fn';
import Widget from './cls';
import helper from './ident';
import viaClause from './clause';
import anon from './anon';
import cfg from './obj';
import fwd, { Widget as FwdWidget, anonFwd } from './barrel';
import * as fnMod from './fn';

export function useDefaults(w: Widget): number {
  main();
  other();
  new Widget().spin();
  w.spin();
  helper();
  viaClause();
  anon();
  fwd();
  new FwdWidget().spin();
  anonFwd();
  fnMod.default();
  return cfg.level;
}
