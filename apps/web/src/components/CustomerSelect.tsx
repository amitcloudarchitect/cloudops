"use client";

import { useCustomer } from "@/lib/customer";

// The customer every customer-specific screen shows: "All customers" or one of them.
// Staff with more than one customer only; a customer never sees it (lib/customer.tsx).
export function CustomerSelect() {
  const { tenantId, setTenantId, selectable, tenants } = useCustomer();
  if (!selectable) return null;
  return (
    <label className="flex items-center gap-2 text-sm text-chrome-muted">
      Customer
      <select
        data-testid="customer-select"
        className="min-h-8 max-w-56 rounded-control border border-line bg-chrome-raised px-2 py-1 text-sm text-chrome-ink hover:border-chrome-ink"
        value={tenantId ?? ""}
        onChange={(e) => setTenantId(e.target.value === "" ? null : e.target.value)}
      >
        <option value="">All customers</option>
        {tenants.map((t) => (
          <option key={t.id} value={t.id}>
            {t.name}
          </option>
        ))}
      </select>
    </label>
  );
}
