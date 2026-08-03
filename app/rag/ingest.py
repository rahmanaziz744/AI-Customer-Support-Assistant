"""Ingest policy markdown into the pgvector-backed corpus.

Idempotent: a document whose content hash is unchanged is skipped, so re-running
ingestion after editing one policy only re-embeds that policy.
"""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.policy import PolicyChunk, PolicyDocument
from app.rag.chunker import chunk_markdown
from app.rag.embeddings import get_embedder
from app.rag.loader import load_policies

logger = get_logger(__name__)


@dataclass
class IngestReport:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    chunks_written: int = 0

    def summary(self) -> str:
        return (
            f"created={self.created} updated={self.updated} "
            f"skipped={self.skipped} chunks={self.chunks_written}"
        )


async def ingest_policies(
    db: AsyncSession, directory: Path, *, force: bool = False
) -> IngestReport:
    policies = load_policies(directory)
    embedder = get_embedder()
    report = IngestReport()

    for policy in policies:
        existing = (
            await db.execute(select(PolicyDocument).where(PolicyDocument.slug == policy.slug))
        ).scalar_one_or_none()

        if existing and existing.content_hash == policy.content_hash and not force:
            report.skipped += 1
            logger.debug("policy_unchanged", slug=policy.slug)
            continue

        if existing:
            # Replace chunks wholesale; partial diffing would risk leaving stale
            # text retrievable after a policy is rewritten.
            await db.execute(delete(PolicyChunk).where(PolicyChunk.document_id == existing.id))
            existing.title = policy.title
            existing.category = policy.category
            existing.version = policy.version
            existing.source_path = policy.source_path
            existing.content_hash = policy.content_hash
            existing.rules = policy.rules
            document = existing
            report.updated += 1
        else:
            document = PolicyDocument(
                slug=policy.slug,
                title=policy.title,
                category=policy.category,
                version=policy.version,
                source_path=policy.source_path,
                content_hash=policy.content_hash,
                rules=policy.rules,
            )
            db.add(document)
            await db.flush()  # assign document.id before chunks reference it
            report.created += 1

        chunks = chunk_markdown(policy.body, policy.title)
        vectors = embedder.embed_documents([c.content for c in chunks])

        for chunk, vector in zip(chunks, vectors, strict=True):
            db.add(
                PolicyChunk(
                    document_id=document.id,
                    chunk_index=chunk.index,
                    heading=chunk.heading,
                    content=chunk.content,
                    category=policy.category,
                    token_estimate=chunk.token_estimate,
                    embedding=vector,
                )
            )
        report.chunks_written += len(chunks)
        logger.info("policy_ingested", slug=policy.slug, chunks=len(chunks))

    await db.flush()
    return report
