import { PaymentClient } from "./payment";

const client = new PaymentClient("https://example.invalid");

export async function checkout(orderId: string): Promise<string> {
  const total: number = "100"; // 意図的な型エラー
  return client.charge({ orderId, amount: total, currency: "JPY" });
}
