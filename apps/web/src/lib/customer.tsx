"use client";

// The customer the console is looking at (the selector in the top bar). It only
// narrows: every API call sends it as `tenant_id`, and the API intersects it with
// the scope it resolved from the signed-in principal (ADR 0002), refusing a tenant
// outside that scope as not found. Choosing a customer here grants nothing.
//
// Only staff whose scope holds more than one customer choose. A customer, or staff
// with one customer, always reads its whole scope: tenantId stays null.

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import type { Me } from "@cloudops/contracts";

import { useSession } from "./session";

export interface CustomerChoice {
  /** The chosen customer, or null for every customer in scope. */
  tenantId: string | null;
  setTenantId: (tenantId: string | null) => void;
  /** Whether this user chooses at all: staff with more than one customer. */
  selectable: boolean;
  /** The customers the user may choose from, by name (from /v1/me). */
  tenants: readonly Me["tenants"][number][];
}

const CustomerContext = createContext<CustomerChoice>({
  tenantId: null,
  setTenantId: () => undefined,
  selectable: false,
  tenants: [],
});

const keyFor = (oid: string) => `cloudops.customer.${oid}`;

function remembered(oid: string): string | null {
  try {
    return window.localStorage.getItem(keyFor(oid));
  } catch {
    return null; // Storage blocked: start from every customer.
  }
}

function remember(oid: string, tenantId: string | null) {
  try {
    if (tenantId === null) window.localStorage.removeItem(keyFor(oid));
    else window.localStorage.setItem(keyFor(oid), tenantId);
  } catch {
    // Storage blocked: the choice lasts for this page.
  }
}

export function CustomerProvider({ children }: { children: ReactNode }) {
  const session = useSession();
  const me = session.status === "ready" ? session.me : null;
  const oid = me?.oid ?? null;
  const tenants = useMemo(() => me?.tenants ?? [], [me]);
  const selectable = me !== null && me.kind === "staff" && tenants.length > 1;
  const [chosen, setChosen] = useState<string | null>(null);

  // On sign-in, pick up this user's last choice, if it is still in their scope.
  useEffect(() => {
    if (oid === null || !selectable) {
      setChosen(null);
      return;
    }
    const last = remembered(oid);
    const valid = last !== null && tenants.some((t) => t.id === last);
    if (last !== null && !valid) remember(oid, null); // revoked or offboarded: forget it
    setChosen(valid ? last : null);
  }, [oid, selectable, tenants]);

  const setTenantId = useCallback(
    (tenantId: string | null) => {
      if (oid === null || !selectable) return;
      const next = tenantId !== null && tenants.some((t) => t.id === tenantId) ? tenantId : null;
      remember(oid, next);
      setChosen(next);
    },
    [oid, selectable, tenants],
  );

  const tenantId = selectable && chosen !== null && tenants.some((t) => t.id === chosen) ? chosen : null;
  const value = useMemo(
    () => ({ tenantId, setTenantId, selectable, tenants }),
    [tenantId, setTenantId, selectable, tenants],
  );
  return <CustomerContext.Provider value={value}>{children}</CustomerContext.Provider>;
}

export function useCustomer(): CustomerChoice {
  return useContext(CustomerContext);
}

/** The query parameter for an API call: absent for every customer. */
export function customerQuery(tenantId: string | null): { tenant_id?: string } {
  return tenantId === null ? {} : { tenant_id: tenantId };
}
