const ZERO_DECIMAL_CURRENCIES = new Set(["JPY", "KRW", "VND", "IDR"]);

export function currencyExponent(currency = "CNY"): number {
  return ZERO_DECIMAL_CURRENCIES.has(currency.toUpperCase()) ? 0 : 2;
}

export function currencyMinorFactor(currency = "CNY"): number {
  return 10 ** currencyExponent(currency);
}

export function majorToMinor(amount: number, currency = "CNY"): number {
  return Math.round(amount * currencyMinorFactor(currency));
}

export function formatMinorMoney(
  amountMinor: number | null | undefined,
  currency = "CNY",
  locale = "en-US",
): string {
  if (amountMinor == null) return "Provider price unavailable";
  return new Intl.NumberFormat(locale, {
    style: "currency",
    currency,
    maximumFractionDigits: currencyExponent(currency),
  }).format(amountMinor / currencyMinorFactor(currency));
}
