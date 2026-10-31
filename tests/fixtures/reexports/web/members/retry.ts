export interface Retry {
  limit: number;
  delay(attempt: number): number;
}

export interface Hooks {
  before: string[];
}
