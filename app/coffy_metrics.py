"""Calculs purs de Pilotage CoFFY, sans contenu de résultat ni effet métier."""

from collections import Counter

TERMINAL = frozenset({"success", "failed", "timeout", "cancelled"})
FAILURES = frozenset({"failed", "timeout", "cancelled"})


def _percent(numerator, denominator):
    return round(100 * numerator / denominator, 1) if denominator else None


def summarize(runs, feedback):
    """Agrège une cohorte filtrée ; les avis portent sur ces seuls runs."""
    statuses = Counter(row["status"] for row in runs)
    terminal = sum(statuses[status] for status in TERMINAL)
    durations = [seconds for row in runs if row["status"] in TERMINAL
                 if row["started_at"] is not None and row["completed_at"] is not None
                 if (seconds := (row["completed_at"] - row["started_at"]).total_seconds()) >= 0]
    errors = Counter(row["error_code"] for row in runs
                     if row["status"] in FAILURES and row["error_code"])
    providers = Counter(row["provider"] for row in runs)
    models = Counter((row["provider"], row["model_name"]) for row in runs)
    successes_by_provider = Counter(row["provider"] for row in runs if row["status"] == "success")
    successes_by_model = Counter((row["provider"], row["model_name"]) for row in runs
                                 if row["status"] == "success")

    latest = {}
    for item in feedback:
        key = item["run_id"], item["author_id"]
        order = item["version"], item["created_at"], item["id"]
        if key not in latest or order > latest[key][0]:
            latest[key] = order, item
    selected = [item for _, item in latest.values()]
    scores = [item["score"] for item in selected]

    return {
        "volume": len(runs),
        "statuses": {status: statuses[status] for status in
                     ("pending", "queued", "running", "retrying", "success", "failed", "timeout", "cancelled")},
        "terminal_count": terminal,
        "success_rate_percent": _percent(statuses["success"], terminal),
        "duration": {"count": len(durations),
                     "mean_seconds": round(sum(durations) / len(durations), 1) if durations else None,
                     "min_seconds": round(min(durations), 1) if durations else None,
                     "max_seconds": round(max(durations), 1) if durations else None},
        "errors": [{"code": code, "count": count} for code, count in
                   sorted(errors.items(), key=lambda pair: (-pair[1], pair[0]))[:5]],
        "by_provider": [{"provider": provider, "runs": count,
                         "success": successes_by_provider[provider]}
                        for provider, count in sorted(providers.items(), key=lambda pair: (-pair[1], pair[0]))],
        "by_model": [{"provider": provider, "model": model, "runs": count,
                      "success": successes_by_model[(provider, model)]}
                     for (provider, model), count in sorted(models.items(),
                         key=lambda pair: (-pair[1], pair[0][0], pair[0][1]))],
        "satisfaction": {"feedback_count": len(scores),
                         "rated_runs": len({item["run_id"] for item in selected}),
                         "mean_score": round(sum(scores) / len(scores), 2) if scores else None,
                         "positive_rate_percent": _percent(sum(score >= 4 for score in scores), len(scores))},
    }
