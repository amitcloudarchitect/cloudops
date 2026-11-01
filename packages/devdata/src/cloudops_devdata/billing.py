"""Give a customer a subscription and months of cloud consumption, as the billing
connectors would. Local only.

    CLOUDOPS_ENV=local python -m cloudops_devdata.billing --tenant <uuid> \\
        --daily 150 --currency EUR --months 7

For the console's commercial setup journey (ADR 0034): a customer onboarded by
the journey has no billing yet, so its margin would stay "not captured: no
billing data" whatever SOW it is given. This loads one consumption line a day,
the same amount every day, from the first day of the month ``--months`` back to
yesterday, under a new Azure subscription named after the tenant. Writes as the
ingest login through packages/tenancy, under the tenant's own scope. Invented
amounts, never a real customer's figures.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import text

from cloudops_config import Component, ConfigurationError, DbLogin, Environment, load_settings
from cloudops_tenancy import Database, Role, TenantScope

_ACCOUNT = text(
    "INSERT INTO cloudops.cloud_account (id, tenant_id, provider, native_id, display_name,"
    " connection_state) VALUES (:id, :t, 'azure', :native, :name, 'healthy')"
)
_LINE = text(
    "INSERT INTO cloudops.cost_fact (tenant_id, account_id, usage_date, invoice_period,"
    " service_native, charge_class, unblended_cost, amortized_cost, currency)"
    " VALUES (:t, :a, :d, :p, 'Virtual Machines', 'consumption', :amount, :amount, :ccy)"
)


def _first_of(months_back: int, today: date) -> date:
    year, month = today.year, today.month - months_back
    while month < 1:
        year, month = year - 1, month + 12
    return date(year, month, 1)


async def load(tenant: uuid.UUID, daily: Decimal, currency: str, months: int) -> int:
    settings = load_settings(os.environ, require={Component.DATABASE_INGEST})
    if settings.environment is not Environment.LOCAL:
        raise ConfigurationError("loading billing by hand is local-only")
    today = datetime.now(UTC).date()
    day = _first_of(months, today)
    account = uuid.uuid4()
    db = Database.create(settings.database_dsn(DbLogin.INGEST), pool_size=1)
    lines = 0
    try:
        async with db.transaction(TenantScope([tenant], Role.INGEST, "devdata:billing")) as conn:
            await conn.execute(_ACCOUNT, {"id": account, "t": tenant,
                                          "native": f"/subscriptions/{uuid.uuid4()}",
                                          "name": "Journey subscription"})
            while day < today:
                await conn.execute(_LINE, {"t": tenant, "a": account, "d": day,
                                           "p": day.strftime("%Y-%m"), "amount": daily,
                                           "ccy": currency})
                lines += 1
                day += timedelta(days=1)
    finally:
        await db.dispose()
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cloudops_devdata.billing")
    parser.add_argument("--tenant", required=True, type=uuid.UUID)
    parser.add_argument("--daily", required=True, help="the consumption per day, a decimal")
    parser.add_argument("--currency", default="EUR")
    parser.add_argument("--months", type=int, default=7)
    args = parser.parse_args(argv)
    try:
        daily = Decimal(args.daily)
    except InvalidOperation:
        print("error: --daily is a decimal amount", file=sys.stderr)
        return 2
    if daily <= 0 or not 1 <= args.months <= 24 or len(args.currency) != 3:
        print("error: a positive amount, 1 to 24 months and a currency code", file=sys.stderr)
        return 2
    try:
        lines = asyncio.run(load(args.tenant, daily, args.currency.upper(), args.months))
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"loaded {lines} consumption lines")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
