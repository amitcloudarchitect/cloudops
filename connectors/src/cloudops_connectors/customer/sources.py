"""The connector sources for one stored customer credential.

Each is an ordinary step 15 source (``core/ingest.py``), bound to the credential
(``identity.Binding``): its accounts resolve to its tenant, its client may name
only them, its ``instance`` names the credential (its own watermarks; a replay
skips other instances' pages), and nothing outside its accounts is read.

- Azure: ``resource`` (Resource Graph), ``cost_daily`` (the query API's daily
  cost; the Sponsorship offer allows no export, ADR 0023) and ``cost_total``.
  ``cost_export`` needs an export into Getronics' storage and ``invoice`` a billing
  account: neither is something a customer's read-only app registration gives.
- AWS: ``cost_total`` (Cost Explorer) for the bound account only (user decision,
  ADR 0027). Config and the CUR need an aggregator or a bucket the customer would
  name; until then they stay on fixtures.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack
from datetime import UTC, date, datetime, timedelta
from typing import Final

import httpx

from cloudops_config import SecretStore
from cloudops_connectors.aws import mapping as aws_mapping
from cloudops_connectors.aws.billing_source import AwsBilling, PayerBilling
from cloudops_connectors.aws.client import CostExplorerClient, InvoicingClient
from cloudops_connectors.aws.sigv4 import AccessKey, SigV4Auth
from cloudops_connectors.azure import mapping as azure_mapping
from cloudops_connectors.azure.auth import ARM_SCOPE, EntraAuth, TokenCredential
from cloudops_connectors.azure.client import ARM, AzureClient
from cloudops_connectors.azure.source import AzureSource
from cloudops_connectors.core import identity, state
from cloudops_connectors.core.ingest import Fetched, PageContext, PageInfo, PageOutcome, Source
from cloudops_connectors.core.raw import RawStore
from cloudops_connectors.customer.credentials import (
    AWS,
    AZURE,
    StoredCredential,
    StoredSecretCredential,
    aws_keys,
)
from cloudops_tenancy import Database

CE: Final = "https://ce.us-east-1.amazonaws.com"
INVOICING: Final = "https://invoicing.us-east-1.api.aws"
AWS_REGION: Final = "us-east-1"  # Cost Explorer, STS and IAM answer here

# Schedule intervals, as the step 15 sources run them (HLD, Integration cadences).
CADENCE: Final[Mapping[str, Mapping[str, timedelta]]] = {
    AZURE: {azure_mapping.ENTITY: timedelta(minutes=15),
            azure_mapping.COST_DAILY: timedelta(days=1),
            azure_mapping.COST_TOTAL: timedelta(days=1)},
    AWS: {aws_mapping.COST_TOTAL: timedelta(days=1)},
}


def queue(credential: StoredCredential) -> str:
    return f"connectors-{credential.provider}-customer-{credential.code}"


def schedule_id(credential: StoredCredential, entity: str) -> str:
    return f"customer-{credential.provider}-{credential.code}-{entity}"


def azure_client(credential: StoredCredential, token: TokenCredential, *, base_url: str = ARM,
                 transport: httpx.AsyncBaseTransport | None = None) -> AzureClient:
    return AzureClient(base_url, auth=EntraAuth(token, ARM_SCOPE, base_url),
                       transport=transport, allowed=frozenset(credential.account_ids))


def azure_source(store: SecretStore, credential: StoredCredential, stack: AsyncExitStack, *,
                 base_url: str = ARM, transport: httpx.AsyncBaseTransport | None = None,
                 token: TokenCredential | None = None,
                 today: Callable[[], date] | None = None) -> AzureSource:
    if token is None:
        stored = StoredSecretCredential(store, credential)
        stack.push_async_callback(stored.close)
        token = stored
    client = azure_client(credential, token, base_url=base_url, transport=transport)
    stack.push_async_callback(client.aclose)
    return AzureSource(client, entities=frozenset(CADENCE[AZURE]), binding=credential.binding,
                       credential=str(credential.id), today=today)


class AwsCustomerSource(Source):
    """``cost_total`` for one account, with that account's own key."""

    source = aws_mapping.SOURCE
    entities = frozenset(CADENCE[AWS])

    def __init__(self, payer: PayerBilling, binding: identity.Binding, credential: str, *,
                 today: Callable[[], date] | None = None) -> None:
        self._billing = AwsBilling([payer], today or (lambda: datetime.now(UTC).date()), binding)
        self._account = payer.payer
        self.instance = f"{payer.cost_explorer.base_url}#credential={credential}"

    def _check(self, entity: str) -> None:
        if entity not in self.entities:
            raise ValueError(f"unknown entity {entity!r}")

    async def scopes(self, db: Database, entity: str) -> list[str]:
        self._check(entity)
        return [self._account]

    async def fetch(self, entity: str, scope: str, since: datetime,
                    cursor: str | None) -> Fetched:
        self._check(entity)
        return await self._billing.fetch(entity, scope, cursor)

    async def inspect(self, raw: RawStore, entity: str, scope: str, ref: str, body: bytes,
                      cursor: str | None) -> PageInfo:
        self._check(entity)
        return await self._billing.inspect(raw, entity, ref, body)

    async def process(self, db: Database, raw: RawStore, entity: str, ref: str,
                      ctx: PageContext) -> PageOutcome:
        self._check(entity)
        if (await raw.meta(ref)).get("instance", self.instance) != self.instance:
            return PageOutcome(state.RunCounts())  # another credential's page, on replay
        return await self._billing.process(db, raw, entity, ref, ctx)


def aws_signer(store: SecretStore, credential: StoredCredential, service: str,
               base_url: str) -> SigV4Auth:
    def keys() -> AccessKey:
        return aws_keys(store, credential)

    return SigV4Auth(keys, AWS_REGION, service, base_url)


def aws_source(store: SecretStore, credential: StoredCredential, stack: AsyncExitStack, *,
               ce_url: str = CE, transport: httpx.AsyncBaseTransport | None = None,
               today: Callable[[], date] | None = None) -> AwsCustomerSource:
    account = credential.account_ids[0]
    ce = CostExplorerClient(account, base_url=ce_url, transport=transport,
                            auth=aws_signer(store, credential, "ce", ce_url))
    stack.push_async_callback(ce.aclose)
    # Never called: the source serves cost_total only. PayerBilling holds both.
    invoicing = InvoicingClient(account, base_url=INVOICING, transport=transport,
                                auth=aws_signer(store, credential, "invoicing", INVOICING))
    stack.push_async_callback(invoicing.aclose)
    return AwsCustomerSource(PayerBilling(account, ce, invoicing), credential.binding,
                             str(credential.id), today=today)


def build(store: SecretStore, credential: StoredCredential, stack: AsyncExitStack) -> Source:
    if credential.provider == AZURE:
        return azure_source(store, credential, stack)
    if credential.provider == AWS:
        return aws_source(store, credential, stack)
    raise ValueError("unknown provider")
