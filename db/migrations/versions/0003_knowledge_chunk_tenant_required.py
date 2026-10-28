"""knowledge_chunk.tenant_id becomes NOT NULL (contract).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-28

ADR 0001. Completes 0002: NULL-tenant rows now live in
cloudops.global_knowledge_chunk, so knowledge_chunk keeps only customer content
and gets the same tenant_isolation policy as every other tenant table — no
`OR tenant_id IS NULL`.

Shipped in the same change as 0002 only because no release has run against
0001 yet (there is no deployment pipeline before step 13). From the first
deployed release on, a contract migration ships a release after its expand.

Safety:
- Only rows already copied to the global table are deleted. Any NULL row that
  was not copied survives the DELETE and makes SET NOT NULL fail, rolling the
  whole migration back rather than losing content.
- NOT NULL goes through a validated CHECK, so SET NOT NULL skips the full-table
  scan under its ACCESS EXCLUSIVE lock (lock_timeout 3s still applies).
- The policy is dropped and recreated in one transaction. Even between the
  two, RLS is FORCEd with no permissive policy, which denies everything.
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

CHANGE_KIND = "contract"

CONTRACT = """
DELETE FROM cloudops.knowledge_chunk k
WHERE k.tenant_id IS NULL
  AND EXISTS (SELECT 1 FROM cloudops.global_knowledge_chunk g WHERE g.id = k.id);

ALTER TABLE cloudops.knowledge_chunk
    ADD CONSTRAINT knowledge_chunk_tenant_required CHECK (tenant_id IS NOT NULL) NOT VALID;
ALTER TABLE cloudops.knowledge_chunk VALIDATE CONSTRAINT knowledge_chunk_tenant_required;
ALTER TABLE cloudops.knowledge_chunk ALTER COLUMN tenant_id SET NOT NULL;
ALTER TABLE cloudops.knowledge_chunk DROP CONSTRAINT knowledge_chunk_tenant_required;

DROP POLICY tenant_isolation ON cloudops.knowledge_chunk;
CREATE POLICY tenant_isolation ON cloudops.knowledge_chunk
  USING      (tenant_id = ANY(cloudops.current_tenant_ids()))
  WITH CHECK (tenant_id = ANY(cloudops.current_tenant_ids()));
"""


def upgrade() -> None:
    op.execute(CONTRACT)


def downgrade() -> None:
    raise NotImplementedError("roll forward, or restore with PITR")
