"""COFFY-05 : calculs de Pilotage sans base de données."""

import unittest
from datetime import datetime, timedelta, timezone

from app.coffy_metrics import summarize

NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


def run(identity, status, *, provider='openai', model='m1', error=None, duration=None):
    return dict(id=identity, status=status, provider=provider, model_name=model,
                error_code=error, started_at=NOW if duration is not None else None,
                completed_at=NOW + timedelta(seconds=duration) if duration is not None else None)


def feedback(identity, run_id, author, version, score, seconds=0):
    return dict(id=identity, run_id=run_id, author_id=author, version=version,
                score=score, created_at=NOW + timedelta(seconds=seconds))


class CoffyMetricsTests(unittest.TestCase):
    def test_absence_de_denominateur_et_de_mesure(self):
        empty = summarize([], [])
        self.assertEqual(empty['volume'], 0)
        self.assertIsNone(empty['success_rate_percent'])
        self.assertIsNone(empty['duration']['mean_seconds'])
        self.assertIsNone(empty['satisfaction']['mean_score'])
        self.assertIsNone(empty['satisfaction']['positive_rate_percent'])
        pending = summarize([run('1', 'pending')], [])
        self.assertEqual(pending['terminal_count'], 0)
        self.assertIsNone(pending['success_rate_percent'])

    def test_volumes_taux_durees_erreurs_et_ventilations(self):
        data = summarize([
            run('1', 'success', duration=10),
            run('2', 'success', duration=20),
            run('3', 'failed', provider='ollama', model='m2', error='invalid_json', duration=30),
            run('4', 'timeout', provider='ollama', model='m2', error='timeout'),
            run('5', 'cancelled', error='permission_revoked'),
            run('6', 'retrying'),
        ], [])
        self.assertEqual(data['volume'], 6)
        self.assertEqual(data['terminal_count'], 5)
        self.assertEqual(data['success_rate_percent'], 40.0)
        self.assertEqual(data['duration'], {'count': 3, 'mean_seconds': 20.0,
                                            'min_seconds': 10.0, 'max_seconds': 30.0})
        self.assertEqual([item['code'] for item in data['errors']],
                         ['invalid_json', 'permission_revoked', 'timeout'])
        self.assertEqual(data['by_provider'][0], {'provider': 'openai', 'runs': 4, 'success': 2})
        self.assertEqual(data['by_model'][1], {'provider': 'ollama', 'model': 'm2',
                                               'runs': 2, 'success': 0})

    def test_dernier_avis_par_auteur_et_run(self):
        data = summarize([run('r1', 'success'), run('r2', 'success')], [
            feedback('a', 'r1', 'u1', 1, 1),
            feedback('b', 'r1', 'u1', 2, 5),
            feedback('c', 'r1', 'u2', 1, 4),
            feedback('d', 'r2', 'u1', 1, 3),
        ])
        self.assertEqual(data['satisfaction'], {'feedback_count': 3, 'rated_runs': 2,
            'mean_score': 4.0, 'positive_rate_percent': 66.7})


if __name__ == '__main__':
    unittest.main()
