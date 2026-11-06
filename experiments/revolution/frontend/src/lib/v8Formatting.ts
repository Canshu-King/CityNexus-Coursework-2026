import { formatMinorMoney } from "./money";

export function formatV8Money(minor: number | null | undefined, currency = "CNY", locale = "en") {
  return formatMinorMoney(minor, currency, locale === "zh-CN" ? "zh-CN" : "en-US");
}

export function formatV8Date(value?: string, locale = "en") {
  return value ? new Intl.DateTimeFormat(locale === "zh-CN" ? "zh-CN" : "en-GB", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value)) : "—";
}
