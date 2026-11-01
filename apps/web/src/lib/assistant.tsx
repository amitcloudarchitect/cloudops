"use client";

// Where the user is, for the AMARA assistant panel (ADR 0029). Each screen declares
// its screen name and, when it knows them, the customer and the record it shows.
// A hint only: the API re-checks the customer and the record under the caller's
// own scope, so nothing here widens what anyone can see.

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode, RefObject } from "react";

import type { ScreenContext } from "@cloudops/contracts";

export type Screen = NonNullable<ScreenContext["screen"]>;
export type RecordType = NonNullable<ScreenContext["record_type"]>;

export interface Where {
  screen: Screen;
  /** The customer the screen is about, when it knows exactly one. */
  tenantId?: string | null;
  record?: { type: RecordType; id: string } | null;
}

const OTHER: Where = { screen: "other" };

interface Assistant {
  where: Where;
  open: boolean;
  setOpen: (open: boolean) => void;
  toggle: () => void;
  /** The header button: focus returns here when the panel closes. */
  launcher: RefObject<HTMLButtonElement | null>;
  declare: (where: Where) => () => void;
}

const AssistantContext = createContext<Assistant | null>(null);

export function AssistantProvider({ children }: { children: ReactNode }) {
  const [where, setWhere] = useState<Where>(OTHER);
  const [open, setOpen] = useState(false);
  const launcher = useRef<HTMLButtonElement | null>(null);
  const toggle = useCallback(() => setOpen((now) => !now), []);
  const declare = useCallback((next: Where) => {
    setWhere(next);
    // Leaving the screen: back to "other", unless another screen has already declared.
    return () => setWhere((now) => (now === next ? OTHER : now));
  }, []);
  const value = useMemo(() => ({ where, open, setOpen, toggle, launcher, declare }), [where, open, toggle, declare]);
  return <AssistantContext.Provider value={value}>{children}</AssistantContext.Provider>;
}

export function useAssistant(): Assistant {
  const value = useContext(AssistantContext);
  if (!value) throw new Error("useAssistant outside AssistantProvider");
  return value;
}

/** Declare where the user is, for as long as the calling screen is shown. */
export function useAssistantContext({ screen, tenantId, record }: Where): void {
  const { declare } = useAssistant();
  const tenant = tenantId ?? null;
  const recordType = record?.type ?? null;
  const recordId = record?.id ?? null;
  useEffect(
    () => declare({ screen, tenantId: tenant, record: recordType && recordId ? { type: recordType, id: recordId } : null }),
    [declare, screen, tenant, recordType, recordId],
  );
}
