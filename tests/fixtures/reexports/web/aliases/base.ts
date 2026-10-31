export interface Base {
  retry: number;
  shared: string;
}

export interface Other {
  shared: number;
  onlyOther: boolean;
}

export class Engine {
  start(): number {
    return 1;
  }
}
