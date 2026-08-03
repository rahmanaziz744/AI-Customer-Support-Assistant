"""initial schema

Revision ID: 86d729aad22e
Revises: 
Create Date: 2026-08-02 16:35:26.774834
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '86d729aad22e'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pgvector must exist before any VECTOR column is created.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table('orders',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('order_ref', sa.String(length=64), nullable=False),
    sa.Column('customer_email', sa.String(length=320), nullable=False),
    sa.Column('status', sa.Enum('PLACED', 'SHIPPED', 'DELIVERED', 'CANCELLED', 'RETURNED', name='order_status', native_enum=False, length=32), nullable=False),
    sa.Column('items', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('total_amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('placed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('delivered_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('tracking_number', sa.String(length=64), nullable=True),
    sa.Column('refunded_amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('is_final_sale', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_orders'))
    )
    op.create_index(op.f('ix_orders_created_at'), 'orders', ['created_at'], unique=False)
    op.create_index(op.f('ix_orders_customer_email'), 'orders', ['customer_email'], unique=False)
    op.create_index(op.f('ix_orders_order_ref'), 'orders', ['order_ref'], unique=True)
    op.create_table('policy_documents',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('slug', sa.String(length=120), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('category', sa.String(length=64), nullable=False),
    sa.Column('version', sa.String(length=32), nullable=False),
    sa.Column('source_path', sa.String(length=500), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('rules', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_policy_documents'))
    )
    op.create_index(op.f('ix_policy_documents_category'), 'policy_documents', ['category'], unique=False)
    op.create_index(op.f('ix_policy_documents_created_at'), 'policy_documents', ['created_at'], unique=False)
    op.create_index(op.f('ix_policy_documents_slug'), 'policy_documents', ['slug'], unique=True)
    op.create_table('tickets',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('channel', sa.Enum('EMAIL', 'WEB', 'CHAT', name='channel', native_enum=False, length=32), nullable=False),
    sa.Column('customer_email', sa.String(length=320), nullable=False),
    sa.Column('customer_name', sa.String(length=200), nullable=True),
    sa.Column('subject', sa.String(length=500), nullable=False),
    sa.Column('body', sa.Text(), nullable=False),
    sa.Column('order_ref', sa.String(length=64), nullable=True),
    sa.Column('status', sa.Enum('NEW', 'PROCESSING', 'AWAITING_APPROVAL', 'ESCALATED', 'RESOLVED', 'REJECTED', 'FAILED', name='ticket_status', native_enum=False, length=32), nullable=False),
    sa.Column('category', sa.Enum('REFUND_REQUEST', 'REPLACEMENT_REQUEST', 'ORDER_STATUS', 'SHIPPING_ISSUE', 'BILLING', 'TECHNICAL_SUPPORT', 'ACCOUNT', 'COMPLAINT', 'GENERAL_INQUIRY', name='ticket_category', native_enum=False, length=32), nullable=True),
    sa.Column('sentiment', sa.Enum('POSITIVE', 'NEUTRAL', 'NEGATIVE', 'VERY_NEGATIVE', name='sentiment', native_enum=False, length=32), nullable=True),
    sa.Column('priority', sa.Integer(), nullable=True),
    sa.Column('confidence', sa.Float(), nullable=True),
    sa.Column('classification_meta', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_tickets'))
    )
    op.create_index(op.f('ix_tickets_category'), 'tickets', ['category'], unique=False)
    op.create_index(op.f('ix_tickets_created_at'), 'tickets', ['created_at'], unique=False)
    op.create_index(op.f('ix_tickets_customer_email'), 'tickets', ['customer_email'], unique=False)
    op.create_index(op.f('ix_tickets_order_ref'), 'tickets', ['order_ref'], unique=False)
    op.create_index(op.f('ix_tickets_priority'), 'tickets', ['priority'], unique=False)
    op.create_index(op.f('ix_tickets_status'), 'tickets', ['status'], unique=False)
    op.create_table('agent_runs',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('ticket_id', sa.UUID(), nullable=False),
    sa.Column('thread_id', sa.String(length=64), nullable=False),
    sa.Column('status', sa.Enum('RUNNING', 'AWAITING_APPROVAL', 'COMPLETED', 'ESCALATED', 'REJECTED', 'FAILED', name='run_status', native_enum=False, length=32), nullable=False),
    sa.Column('draft_response', sa.Text(), nullable=True),
    sa.Column('final_response', sa.Text(), nullable=True),
    sa.Column('proposed_actions', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('executed_actions', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('policy_citations', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('guardrail_flags', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('eligibility', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('escalation_reason', sa.Text(), nullable=True),
    sa.Column('prompt_versions', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('total_input_tokens', sa.Integer(), nullable=False),
    sa.Column('total_output_tokens', sa.Integer(), nullable=False),
    sa.Column('total_cost_usd', sa.Numeric(precision=12, scale=6), nullable=False),
    sa.Column('approved_by', sa.String(length=200), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id'], name=op.f('fk_agent_runs_ticket_id_tickets'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_runs'))
    )
    op.create_index(op.f('ix_agent_runs_created_at'), 'agent_runs', ['created_at'], unique=False)
    op.create_index(op.f('ix_agent_runs_status'), 'agent_runs', ['status'], unique=False)
    op.create_index(op.f('ix_agent_runs_thread_id'), 'agent_runs', ['thread_id'], unique=True)
    op.create_index(op.f('ix_agent_runs_ticket_id'), 'agent_runs', ['ticket_id'], unique=False)
    op.create_table('order_actions',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('order_id', sa.UUID(), nullable=False),
    sa.Column('ticket_id', sa.UUID(), nullable=True),
    sa.Column('action_type', sa.Enum('REFUND', 'REPLACEMENT', name='action_type', native_enum=False, length=32), nullable=False),
    sa.Column('status', sa.Enum('PROPOSED', 'EXECUTED', 'FAILED', name='action_status', native_enum=False, length=32), nullable=False),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=True),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.Column('approved_by', sa.String(length=200), nullable=True),
    sa.Column('idempotency_key', sa.String(length=120), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['order_id'], ['orders.id'], name=op.f('fk_order_actions_order_id_orders'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id'], name=op.f('fk_order_actions_ticket_id_tickets'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_order_actions'))
    )
    op.create_index(op.f('ix_order_actions_idempotency_key'), 'order_actions', ['idempotency_key'], unique=True)
    op.create_index(op.f('ix_order_actions_order_id'), 'order_actions', ['order_id'], unique=False)
    op.create_index(op.f('ix_order_actions_ticket_id'), 'order_actions', ['ticket_id'], unique=False)
    op.create_table('policy_chunks',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('document_id', sa.UUID(), nullable=False),
    sa.Column('chunk_index', sa.Integer(), nullable=False),
    sa.Column('heading', sa.String(length=300), nullable=True),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('category', sa.String(length=64), nullable=False),
    sa.Column('token_estimate', sa.Integer(), nullable=False),
    sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=384), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['policy_documents.id'], name=op.f('fk_policy_chunks_document_id_policy_documents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_policy_chunks')),
    sa.UniqueConstraint('document_id', 'chunk_index', name='chunk_index')
    )
    op.create_index(op.f('ix_policy_chunks_category'), 'policy_chunks', ['category'], unique=False)
    op.create_index(op.f('ix_policy_chunks_document_id'), 'policy_chunks', ['document_id'], unique=False)
    # HNSW rather than IVFFlat: it needs no training pass, so it stays correct
    # when built on an empty table and re-ingested into later.
    op.execute(
        "CREATE INDEX ix_policy_chunks_embedding ON policy_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.create_table('agent_traces',
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('run_id', sa.UUID(), nullable=False),
    sa.Column('ticket_id', sa.UUID(), nullable=False),
    sa.Column('step_index', sa.Integer(), nullable=False),
    sa.Column('node_name', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('model', sa.String(length=64), nullable=True),
    sa.Column('input_summary', sa.Text(), nullable=True),
    sa.Column('output_summary', sa.Text(), nullable=True),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('cache_read_tokens', sa.Integer(), nullable=False),
    sa.Column('cache_write_tokens', sa.Integer(), nullable=False),
    sa.Column('cost_usd', sa.Numeric(precision=12, scale=6), nullable=True),
    sa.Column('latency_ms', sa.Integer(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('meta', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['agent_runs.id'], name=op.f('fk_agent_traces_run_id_agent_runs'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['ticket_id'], ['tickets.id'], name=op.f('fk_agent_traces_ticket_id_tickets'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_agent_traces'))
    )
    op.create_index(op.f('ix_agent_traces_created_at'), 'agent_traces', ['created_at'], unique=False)
    op.create_index(op.f('ix_agent_traces_node_name'), 'agent_traces', ['node_name'], unique=False)
    op.create_index(op.f('ix_agent_traces_run_id'), 'agent_traces', ['run_id'], unique=False)
    op.create_index(op.f('ix_agent_traces_ticket_id'), 'agent_traces', ['ticket_id'], unique=False)
    # ### end Alembic commands ###


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_policy_chunks_embedding")
    op.drop_index(op.f('ix_agent_traces_ticket_id'), table_name='agent_traces')
    op.drop_index(op.f('ix_agent_traces_run_id'), table_name='agent_traces')
    op.drop_index(op.f('ix_agent_traces_node_name'), table_name='agent_traces')
    op.drop_index(op.f('ix_agent_traces_created_at'), table_name='agent_traces')
    op.drop_table('agent_traces')
    op.drop_index(op.f('ix_policy_chunks_document_id'), table_name='policy_chunks')
    op.drop_index(op.f('ix_policy_chunks_category'), table_name='policy_chunks')
    op.drop_table('policy_chunks')
    op.drop_index(op.f('ix_order_actions_ticket_id'), table_name='order_actions')
    op.drop_index(op.f('ix_order_actions_order_id'), table_name='order_actions')
    op.drop_index(op.f('ix_order_actions_idempotency_key'), table_name='order_actions')
    op.drop_table('order_actions')
    op.drop_index(op.f('ix_agent_runs_ticket_id'), table_name='agent_runs')
    op.drop_index(op.f('ix_agent_runs_thread_id'), table_name='agent_runs')
    op.drop_index(op.f('ix_agent_runs_status'), table_name='agent_runs')
    op.drop_index(op.f('ix_agent_runs_created_at'), table_name='agent_runs')
    op.drop_table('agent_runs')
    op.drop_index(op.f('ix_tickets_status'), table_name='tickets')
    op.drop_index(op.f('ix_tickets_priority'), table_name='tickets')
    op.drop_index(op.f('ix_tickets_order_ref'), table_name='tickets')
    op.drop_index(op.f('ix_tickets_customer_email'), table_name='tickets')
    op.drop_index(op.f('ix_tickets_created_at'), table_name='tickets')
    op.drop_index(op.f('ix_tickets_category'), table_name='tickets')
    op.drop_table('tickets')
    op.drop_index(op.f('ix_policy_documents_slug'), table_name='policy_documents')
    op.drop_index(op.f('ix_policy_documents_created_at'), table_name='policy_documents')
    op.drop_index(op.f('ix_policy_documents_category'), table_name='policy_documents')
    op.drop_table('policy_documents')
    op.drop_index(op.f('ix_orders_order_ref'), table_name='orders')
    op.drop_index(op.f('ix_orders_customer_email'), table_name='orders')
    op.drop_index(op.f('ix_orders_created_at'), table_name='orders')
    op.drop_table('orders')
    # ### end Alembic commands ###
