import { stop } from './consts';
import type { Hooks, Retry } from './retry';

export interface Opts {
  retry: Retry;
  limit: string;
  nested: { depth: number; inner: { leaf: boolean } };
  count: number;
}

/** Options written as a type literal. */
export type KyOptions = {
  /** Retry options. */
  retry?: Retry | number;
  hooks: Hooks;
  inline: {
    before: string[];
    after(): void;
  };
  timeout: number;
  readonly stop: typeof stop;
};

export interface Options extends KyOptions {
  method: string;
}

export type Shape = { kind: 'a'; a: number } | { kind: 'b'; b: string };
