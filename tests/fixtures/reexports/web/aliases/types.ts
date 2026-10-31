import { Base, Engine, Other } from './base';

export type EngineAlias = Engine;
export type Options = Base & { extra: string; tick(): void };
export type Box<T> = Base & { value: T };
export type Either = Base | Other;
export type MaybeBase = Base | null | undefined;
export type Loose = Partial<Base>;
export type Fn = (x: number) => number;
export type Chain = Options;
export type Bag = { [key: string]: number };

export interface Derived extends Options {
  own: number;
}
