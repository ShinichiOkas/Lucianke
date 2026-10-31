import { add } from './math';

function own(): number {
  return 3;
}

export { add as plus, own as ownAlias };
