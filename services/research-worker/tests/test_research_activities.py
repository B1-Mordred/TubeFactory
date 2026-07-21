from research_worker.research_activities import detect_dependent_sources


def source(source_id: str, content_hash: str, chunk_hash: str, embedding: list[float]):
    return {
        "source_document_id": source_id,
        "content_hash": content_hash,
        "chunks": [{"chunk_hash": chunk_hash, "embedding": embedding}],
    }


def test_semantically_near_duplicate_sources_are_marked_dependent() -> None:
    relationships, dependent = detect_dependent_sources(
        [
            source("source-a", "content-a", "chunk-a", [1.0, 0.0, 0.0]),
            source("source-b", "content-b", "chunk-b", [0.98, 0.10, 0.0]),
            source("source-c", "content-c", "chunk-c", [0.0, 0.0, 1.0]),
        ]
    )
    assert relationships == [
        {
            "source_document_id": "source-a",
            "related_source_document_id": "source-b",
            "relationship": "near_duplicate",
            "confidence": 99,
            "reason": "local feature-hash chunk cosine similarity 0.995",
        }
    ]
    assert dependent == {"source-a", "source-b"}


def test_shared_chunk_hash_is_an_exact_duplicate_even_with_different_content_hashes() -> None:
    relationships, dependent = detect_dependent_sources(
        [
            source("source-a", "content-a", "shared", [1.0, 0.0]),
            source("source-b", "content-b", "shared", [0.0, 1.0]),
        ]
    )
    assert relationships[0]["relationship"] == "exact_duplicate"
    assert relationships[0]["confidence"] == 100
    assert dependent == {"source-a", "source-b"}
