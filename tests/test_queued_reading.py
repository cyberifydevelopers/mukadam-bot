"""The sync only registers new mail (stage "queued", visible at once); a background worker then reads it.

Gmail and the invoice graph are mocked; nothing is sent and the real database is never touched.
"""

import time
import unittest
from datetime import datetime
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import pipeline
from app.database import Base
from app.models import ProcessedEmail
from app.services.gmail_service import FetchedEmail


def fetched(message_id):
    return FetchedEmail(
        message_id=message_id, sender="bank@meezanbank.com", subject="LC", received_at=datetime.utcnow(),
        attachment_paths=["/x/id_lc.pdf"], body_text="", image_paths=[],
    )


class QueuedReading(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine)
        patches = [
            mock.patch.object(pipeline, "SessionLocal", self.Session),
            mock.patch.object(pipeline, "_sender_not_allowed", return_value=None),
            mock.patch.object(pipeline, "_documents_to_read", return_value=None),
            mock.patch.object(pipeline.gmail_service, "fetch_email", side_effect=fetched),
            mock.patch.object(pipeline.gmail_service, "chain_senders", return_value=[]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def wait_for_worker(self):
        for _ in range(100):
            if not pipeline._workers:
                return
            time.sleep(0.05)
        self.fail("the worker never finished")

    def test_mail_is_queued_at_once_then_read_by_the_worker(self):
        seen = []

        def graph(db, email, _fetched):
            seen.append(email.stage)
            email.stage = "done"
            db.commit()
            return 1

        with self.Session() as db, mock.patch.object(pipeline, "_run_graph", side_effect=graph):
            with mock.patch.object(pipeline, "_start_worker"):  # sync alone: registered, not read
                self.assertEqual(pipeline._register_message(db, "m1"), 1)
            self.assertEqual(db.query(ProcessedEmail).one().stage, "queued")

            pipeline._start_worker()
            self.wait_for_worker()
            db.expire_all()
            self.assertEqual(seen, ["reading"])
            self.assertEqual(db.query(ProcessedEmail).one().stage, "done")

    def test_a_mail_with_no_attachment_is_read_from_its_body(self):
        """The worker fetches the mail again, so it must make the body document again too: the graph reads attachment_paths."""
        seen = []

        def bodyless(message_id):
            return FetchedEmail(message_id=message_id, sender="a@b.c", subject="", received_at=datetime.utcnow(),
                                attachment_paths=[], body_text="Please deliver", image_paths=[])

        def graph(db, email, fetched_email):
            seen.append(list(fetched_email.attachment_paths))
            email.stage = "done"
            db.commit()
            return 1

        with self.Session() as db, \
             mock.patch.object(pipeline.gmail_service, "fetch_email", side_effect=bodyless), \
             mock.patch.object(pipeline, "_documents_to_read", side_effect=lambda f: f.attachment_paths.append("/x/id_email_body.txt")), \
             mock.patch.object(pipeline, "_run_graph", side_effect=graph):
            with mock.patch.object(pipeline, "_start_worker"):
                pipeline._register_message(db, "m1")
            pipeline._start_worker()
            self.wait_for_worker()
        self.assertEqual(seen, [["/x/id_email_body.txt"]])

    def test_a_failing_mail_is_marked_failed_and_the_next_one_still_runs(self):
        def graph(db, email, _fetched):
            if email.message_id == "m1":
                raise RuntimeError("boom")
            email.stage = "done"
            db.commit()
            return 1

        with self.Session() as db, mock.patch.object(pipeline, "_run_graph", side_effect=graph):
            with mock.patch.object(pipeline, "_start_worker"):
                pipeline._register_message(db, "m1")
                pipeline._register_message(db, "m2")
            pipeline._start_worker()
            self.wait_for_worker()
            db.expire_all()
            stages = {e.message_id: e.stage for e in db.query(ProcessedEmail)}
            self.assertEqual(stages, {"m1": "failed", "m2": "done"})


if __name__ == "__main__":
    unittest.main()
