"""Evaluation utilities: path matching, pass^k computation."""

import math


def paths_match(actual: list[str], expected: list[str]) -> bool:
    """Exact sequence match. Both paths must be identical."""
    return actual == expected


def _pass_hat_k_one(num_trials: int, success_count: int, k: int) -> float:
    """
    Unbiased estimator for pass^k for a single task: C(s, k) / C(n, k).
    UMVUE for p^k when each trial is Bernoulli(p). Returns 0 when success_count < k.
    """
    if num_trials < k or success_count < k:
        return 0.0
    return math.comb(success_count, k) / math.comb(num_trials, k)


def compute_pass_k(results: list[bool], k: int) -> float:
    """
    pass^k as unbiased estimator: mean over tasks of C(s, k) / C(n, k).
    results: list of bools, one per trial; grouped into chunks of k per (scenario, test_case, condition).
    Reference: https://arxiv.org/pdf/2406.12045
    """
    if not results or k <= 0:
        return 0.0
    n_cases = len(results) // k
    if n_cases * k != len(results):
        return 0.0
    total = 0.0
    for i in range(n_cases):
        chunk = results[i * k : (i + 1) * k]
        n = len(chunk)
        s = sum(1 for b in chunk if b)
        total += _pass_hat_k_one(n, s, k)
    return total / n_cases if n_cases else 0.0


def compute_pass_at_least_once(results: list[bool], k: int) -> float:
    """pass^1: fraction that passed at least one of k trials."""
    if not results or k <= 0:
        return 0.0
    n_cases = len(results) // k
    if n_cases * k != len(results):
        return 0.0
    passed = 0
    for i in range(n_cases):
        chunk = results[i * k : (i + 1) * k]
        if any(chunk):
            passed += 1
    return passed / n_cases if n_cases else 0.0
