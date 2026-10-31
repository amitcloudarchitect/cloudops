// Brand backgrounds (ADR 0024): Getronics' official Teams backgrounds, from the
// marketing team's brand assets. Used only where nothing is read over them: the
// sign-in page and the states before the console opens. Data screens stay plain,
// as the brand book asks ("do not overlay text on busy backgrounds") and as
// Getronics' internal tools are. Decorative: empty alt text.
//
// Each file carries the full-colour logo in its top-right corner. The book forbids
// cropping the logo, so every crop is anchored top-right (object-right-top): the
// image loses its left side first, never that corner. Nothing is placed over it.

import type { ReactNode } from "react";

import atrium from "@/brand/background-atrium.webp";
import building from "@/brand/background-building.webp";
import sunset from "@/brand/background-sunset.webp";

const BACKGROUNDS = { atrium, building, sunset } as const;
export type Background = keyof typeof BACKGROUNDS;

function Picture({ name, extra = "" }: { name: Background; extra?: string }) {
  const image = BACKGROUNDS[name];
  return (
    <img
      src={image.src}
      width={image.width}
      height={image.height}
      alt=""
      className={`absolute inset-0 size-full object-cover object-right-top ${extra}`}
    />
  );
}

/** A background filling its box: one image, or a light-mode and a dark-mode pair. */
export function BrandPhoto({
  photo,
  dark,
  className = "",
  children,
}: {
  photo: Background;
  /** Shown instead in dark mode. */
  dark?: Background;
  className?: string;
  children?: ReactNode;
}) {
  return (
    <div className={`relative overflow-hidden bg-chrome ${className}`}>
      {dark ? (
        <>
          <Picture name={photo} extra="dark:hidden" />
          <Picture name={dark} extra="hidden dark:block" />
        </>
      ) : (
        <Picture name={photo} />
      )}
      {children ? <div className="relative h-full">{children}</div> : null}
    </div>
  );
}
