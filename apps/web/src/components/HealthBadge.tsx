import { BAND_LABEL, type Band } from "@/lib/health";

const STYLE: Record<Band, string> = {
  healthy: "bg-band-healthy-bg text-band-healthy border-band-healthy",
  watch: "bg-band-watch-bg text-band-watch border-band-watch",
  "at-risk": "bg-band-at-risk-bg text-band-at-risk border-band-at-risk",
  critical: "bg-band-critical-bg text-band-critical border-band-critical",
  unknown: "bg-band-unknown-bg text-band-unknown border-band-unknown",
};

// The band is named in text, never colour alone (WCAG 1.4.1).
export function HealthBadge({ band, label }: { band: Band; label?: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-semibold whitespace-nowrap ${STYLE[band]}`}
      data-band={band}
    >
      <span aria-hidden="true" className="inline-block size-2 rounded-full bg-current" />
      {label ?? BAND_LABEL[band]}
    </span>
  );
}

export const BAND_EDGE: Record<Band, string> = {
  healthy: "border-l-band-healthy",
  watch: "border-l-band-watch",
  "at-risk": "border-l-band-at-risk",
  critical: "border-l-band-critical",
  unknown: "border-l-band-unknown",
};
