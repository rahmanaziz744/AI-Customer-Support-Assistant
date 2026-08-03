"""Similarity search over the policy corpus."""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.policy import PolicyChunk, PolicyDocument
from app.rag.embeddings import get_embedder

# Categories with no policy area of their own. Filtering to them would hide the
# document the ticket actually needs — an eval case asking a shipping question
# under GENERAL_INQUIRY could only ever retrieve the escalation/tone document.
UNSCOPED_CATEGORIES = {"GENERAL_INQUIRY", "COMPLAINT"}

# Applies to every ticket, so it is always kept in the candidate set.
GLOBAL_POLICY_SLUG = "escalation-and-tone"


@dataclass
class RetrievedChunk:
    chunk_id: str
    document_slug: str
    document_title: str
    category: str
    heading: str | None
    content: str
    score: float
    rules: dict[str, Any] = field(default_factory=dict)

    def as_citation(self) -> dict[str, Any]:
        """Compact form stored on the run and rendered in the UI."""
        return {
            "chunk_id": self.chunk_id,
            "document": self.document_title,
            "slug": self.document_slug,
            "heading": self.heading,
            "score": round(self.score, 4),
        }


async def retrieve_policy(
    db: AsyncSession,
    query: str,
    *,
    category: str | None = None,
    top_k: int | None = None,
    include_global: bool = True,
) -> list[RetrievedChunk]:
    """Return the most similar policy chunks to `query`, best first.

    `category` biases retrieval to the ticket's own policy area. The global
    escalation/tone document is always eligible, since its rules apply to every
    ticket regardless of category.
    """
    settings = get_settings()
    k = top_k or settings.retrieval_top_k
    embedder = get_embedder()
    query_vector = embedder.embed_query(query)

    # pgvector's cosine_distance is in [0, 2]; similarity = 1 - distance.
    distance = PolicyChunk.embedding.cosine_distance(query_vector).label("distance")

    stmt = (
        select(PolicyChunk, PolicyDocument, distance)
        .join(PolicyDocument, PolicyChunk.document_id == PolicyDocument.id)
        .order_by(distance)
    )

    if category and category not in UNSCOPED_CATEGORIES:
        allowed = [category]
        if include_global:
            allowed.append("GENERAL_INQUIRY")
        # Over-fetch, then trim: a category filter that matches nothing would
        # otherwise return an empty result rather than the next-best policy.
        stmt = stmt.where(PolicyChunk.category.in_(allowed)).limit(k)
    else:
        stmt = stmt.limit(k)

    rows = (await db.execute(stmt)).all()

    if include_global:
        rows = await _ensure_global_policy(db, rows, distance, k)

    if not rows and category:
        # Category had no chunks at all — fall back to an unfiltered search
        # rather than handing the drafting node nothing to ground itself in.
        rows = (
            await db.execute(
                select(PolicyChunk, PolicyDocument, distance)
                .join(PolicyDocument, PolicyChunk.document_id == PolicyDocument.id)
                .order_by(distance)
                .limit(k)
            )
        ).all()

    return [
        RetrievedChunk(
            chunk_id=str(chunk.id),
            document_slug=doc.slug,
            document_title=doc.title,
            category=chunk.category,
            heading=chunk.heading,
            content=chunk.content,
            score=1.0 - float(dist),
            rules=doc.rules or {},
        )
        for chunk, doc, dist in rows
    ]


async def _ensure_global_policy(db: AsyncSession, rows: list, distance, k: int) -> list:
    """Guarantee the escalation/tone document is among the candidates.

    Its rules — always-escalate topics, the human-approval requirement, response
    standards — govern every ticket regardless of category, so it belongs in the
    drafting context even when a category-specific policy scores higher. Without
    this, a complaint about a media threat could be drafted with no sight of the
    rule saying it must be escalated.
    """
    if any(doc.slug == GLOBAL_POLICY_SLUG for _chunk, doc, _dist in rows):
        return rows

    best_global = (
        await db.execute(
            select(PolicyChunk, PolicyDocument, distance)
            .join(PolicyDocument, PolicyChunk.document_id == PolicyDocument.id)
            .where(PolicyDocument.slug == GLOBAL_POLICY_SLUG)
            .order_by(distance)
            .limit(1)
        )
    ).all()

    if not best_global:
        return rows

    # Displace the weakest category hit rather than growing the context.
    combined = rows[: max(k - 1, 0)] + best_global
    return sorted(combined, key=lambda row: row[2])


def format_citations_for_prompt(chunks: list[RetrievedChunk]) -> str:
    """Render retrieved chunks as numbered, quotable sources for the drafting prompt."""
    if not chunks:
        return "(no policy documents were retrieved)"
    blocks = []
    for i, chunk in enumerate(chunks, start=1):
        label = f"[{i}] {chunk.document_title}"
        if chunk.heading:
            label += f" — {chunk.heading}"
        blocks.append(f"{label}\n{chunk.content}")
    return "\n\n---\n\n".join(blocks)
