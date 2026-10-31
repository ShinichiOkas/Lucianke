import { Base } from './base';

type Mine = Base & { tag: string };

export function same(m: Mine): number {
  return m.retry + m.tag.length;
}
