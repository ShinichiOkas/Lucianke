export class Meter {
  hits = 0;

  reset(): void {
    this.hits = 0;
  }
}

export const meter = new Meter();
