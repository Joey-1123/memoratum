"""Evaluation result comparison contract (RED)."""


def test_compare_results_renders_dataset_backend_rows() -> None:
    from memoratum.eval_compare import compare_results

    report = compare_results(
        [
            {
                "dataset": "locomo-mc10-dialogs",
                "n": 10,
                "accuracy": 0.8,
                "balanced_accuracy": 0.75,
                "manifest": {"vector_store": "sqlite", "embedder": "HashEmbedder:64"},
            },
            {
                "dataset": "longmemeval-s",
                "n": 5,
                "aggregate": {"documents": {"MRR": 0.4}},
                "manifest": {"vector_store": "qdrant", "embedder": "ApiEmbedder:768"},
                "modes": ["documents"],
            },
        ]
    )
    assert "locomo-mc10-dialogs" in report
    assert "qdrant" in report
    assert "MRR" in report
