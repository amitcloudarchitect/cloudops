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

export function dateTime(iso: string | null | undefined): string {
  return iso ? DATE_TIME.format(new Date(iso)) : "—";
}

export function date(iso: string | null | undefined): string {
  return iso ? DATE.format(new Date(iso)) : "—";
}

export function count(value: number): string {
  return new Intl.NumberFormat("en-GB").format(value);
}

/** Round a decimal string half away from zero to `places`, and group thousands. */
export function decimal(value: string, places = 2): string {
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(value.trim());
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
