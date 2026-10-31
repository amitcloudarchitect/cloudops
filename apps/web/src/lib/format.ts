// Presentation formatting. Money and other decimals arrive from the API as
// strings and stay strings: rounding happens once, here, on the digits, never
// through a float (CLAUDE.md, rule 5).

const DATE_TIME = new Intl.DateTimeFormat("en-GB", {
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  timeZone: "UTC",
  timeZoneName: "short",
});
const DATE = new Intl.DateTimeFormat("en-GB", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" });
const MONTH = new Intl.DateTimeFormat("en-GB", { month: "long", year: "numeric", timeZone: "UTC" });

export function dateTime(iso: string | null | undefined): string {
  return iso ? DATE_TIME.format(new Date(iso)) : "—";
}

export function date(iso: string | null | undefined): string {
  return iso ? DATE.format(new Date(iso)) : "—";
}

/** A calendar month, from its first day: "September 2026". */
export function month(iso: string): string {
  return MONTH.format(new Date(iso));
}

export function count(value: number): string {
  return new Intl.NumberFormat("en-GB").format(value);
}

/** A decimal string in plain notation. The API may send Python's exponent form
 * ("0E-8", "1.5E+3"); the point is moved on the digits, never through a float. */
export function plain(value: string): string {
  const text = value.trim();
  const match = /^([+-]?)(\d*)(?:\.(\d*))?[eE]([+-]?\d+)$/.exec(text);
  if (!match) return text.startsWith("+") ? text.slice(1) : text;
  const [, sign = "", whole = "", fraction = "", exp = "0"] = match;
  const digits = `${whole}${fraction}` || "0";
  const point = whole.length + Number(exp);
  let out: string;
  if (point <= 0) out = `0.${"0".repeat(-point)}${digits}`;
  else if (point >= digits.length) out = `${digits}${"0".repeat(point - digits.length)}`;
  else out = `${digits.slice(0, point)}.${digits.slice(point)}`;
  out = out.replace(/^0+(?=\d)/, "");
  return `${sign === "-" ? "-" : ""}${out}`;
}

/** Round a decimal string half away from zero to `places`, and group thousands. */
export function decimal(value: string, places = 2): string {
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(plain(value));
  if (!match) throw new Error(`not a decimal: ${value}`);
  const [, sign = "", whole = "0", fraction = ""] = match;
  const padded = (fraction + "0".repeat(places + 1)).slice(0, places + 1);
  let digits = (whole + padded.slice(0, places)).split("").map(Number);
  if (Number(padded.charAt(places)) >= 5) {
    let i = digits.length - 1;
    while (i >= 0) {
      const next = (digits[i] ?? 0) + 1;
      digits[i] = next % 10;
      if (next < 10) break;
      i -= 1;
    }
    if (i < 0) digits = [1, ...digits];
  }
  const text = digits.join("");
  const intPart = text.slice(0, text.length - places) || "0";
  const fracPart = places > 0 ? `.${text.slice(text.length - places)}` : "";
  const grouped = intPart.replace(/^0+(?=\d)/, "").replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const isZero = /^[0,]*$/.test(grouped) && /^\.?0*$/.test(fracPart);
  return `${sign && !isZero ? "-" : ""}${grouped}${fracPart}`;
}

export function money(value: string, currency: string, places = 2): string {
  return `${currency} ${decimal(value, places)}`;
}

/** A ratio as a decimal string ("0.6667") shown as a percentage ("66.7%"), on its
 * digits: the point moves two places, then rounds once. */
export function percent(ratio: string, places = 1): string {
  const match = /^(-?)(\d+)(?:\.(\d*))?$/.exec(plain(ratio));
  if (!match) throw new Error(`not a decimal: ${ratio}`);
  const [, sign = "", whole = "0", fraction = ""] = match;
  const padded = `${fraction}00`;
  return `${decimal(`${sign}${whole}${padded.slice(0, 2)}.${padded.slice(2) || "0"}`, places)}%`;
}

/** Seconds as the largest unit and the next: "45 s", "12 min", "2 h 5 min", "4 d 3 h". */
export function duration(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  const units: readonly (readonly [number, string])[] = [
    [86_400, "d"],
    [3_600, "h"],
    [60, "min"],
    [1, "s"],
  ];
  const i = units.findIndex(([size]) => s >= size);
  const major = units[i];
  if (!major) return "0 s";
  const minor = units[i + 1];
  const whole = Math.floor(s / major[0]);
  const part = minor ? Math.floor((s % major[0]) / minor[0]) : 0;
  return minor && part > 0 ? `${whole} ${major[1]} ${part} ${minor[1]}` : `${whole} ${major[1]}`;
}

/** A link from ingested data (a SharePoint webUrl) is followed only if it is https:
 * the source is attacker-influenceable, and any other scheme is shown as text. */
export function safeHttps(url: unknown): string | null {
  if (typeof url !== "string") return null;
  try {
    return new URL(url).protocol === "https:" ? url : null;
  } catch {
    return null;
  }
}
