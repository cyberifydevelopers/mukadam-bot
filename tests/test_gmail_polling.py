"""Gmail is checked on a timer even while Pub/Sub works, so a silent Pub/Sub failure never leaves mail waiting for the
dashboard's "Check Gmail now" button."""

import unittest
from unittest import mock

from app.worker import scheduler


def settings(safety=120, reconcile=900):
    return mock.patch.object(scheduler, "settings", mock.Mock(
        gmail_safety_poll_seconds=safety, gmail_reconcile_interval_seconds=reconcile, gmail_watch_renew_interval_seconds=43200))


class PollInterval(unittest.TestCase):
    def test_a_safety_check_runs_even_when_pubsub_works(self):
        with settings():
            self.assertEqual(scheduler.poll_interval(pubsub_active=True), 120)

    def test_without_pubsub_the_shorter_of_the_two_is_used(self):
        with settings(safety=120, reconcile=900):
            self.assertEqual(scheduler.poll_interval(pubsub_active=False), 120)
        with settings(safety=600, reconcile=300):
            self.assertEqual(scheduler.poll_interval(pubsub_active=False), 300)

    def test_polling_off_for_the_fallback_still_leaves_the_safety_check(self):
        with settings(safety=120, reconcile=0):
            self.assertEqual(scheduler.poll_interval(pubsub_active=False), 120)

    def test_everything_off_means_no_polling(self):
        with settings(safety=0, reconcile=0):
            self.assertEqual(scheduler.poll_interval(pubsub_active=False), 0)
        with settings(safety=0, reconcile=900):
            self.assertEqual(scheduler.poll_interval(pubsub_active=True), 0)


class Startup(unittest.TestCase):
    def start(self, pubsub_active, **kw):
        fake = mock.Mock(running=False)
        with settings(**kw), mock.patch.object(scheduler, "_scheduler", fake):
            scheduler.start_scheduler(pubsub_active)
        return {c.kwargs["id"]: c.kwargs for c in fake.add_job.call_args_list}, fake

    def test_the_check_is_scheduled_with_pubsub_working_too(self):
        jobs, fake = self.start(True)
        self.assertEqual(jobs["gmail_reconcile"]["seconds"], 120)
        self.assertIn("gmail_watch_renew", jobs)
        fake.start.assert_called_once()

    def test_the_check_is_scheduled_without_pubsub(self):
        jobs, _ = self.start(False, safety=0, reconcile=900)
        self.assertEqual(jobs["gmail_reconcile"]["seconds"], 900)

    def test_no_check_when_both_are_off_but_the_watch_still_renews(self):
        jobs, _ = self.start(True, safety=0)
        self.assertNotIn("gmail_reconcile", jobs)
        self.assertIn("gmail_watch_renew", jobs)

    def test_the_job_runs_the_gmail_sync(self):
        with mock.patch.object(scheduler, "SessionLocal"), mock.patch.object(scheduler, "sync_gmail", return_value=2) as sync:
            scheduler._reconcile_job()
        sync.assert_called_once()

    def test_a_failed_check_is_logged_not_raised(self):
        with mock.patch.object(scheduler, "SessionLocal"), mock.patch.object(scheduler, "sync_gmail", side_effect=RuntimeError("net down")):
            scheduler._reconcile_job()  # must not raise: the next tick tries again


if __name__ == "__main__":
    unittest.main()
