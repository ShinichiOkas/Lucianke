export interface PaymentRequest {
  orderId: string;
  amount: number;
  currency: "JPY" | "USD";
}

export class PaymentClient {
  constructor(private readonly baseUrl: string) {}

  charge(req: PaymentRequest): Promise<string>;
  charge(req: PaymentRequest, retries: number): Promise<string>;
  /** 決済を要求する。amount は 0 より大きいこと。 */
  async charge(req: PaymentRequest, retries = 0): Promise<string> {
    if (req.amount <= 0) {
      throw new Error("amount must be > 0");
    }
    return `${this.baseUrl}/charge/${req.orderId}?r=${retries}`;
  }
}
