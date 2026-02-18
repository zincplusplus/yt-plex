import importlib
import os
import sys
import tempfile
import unittest


class TestVideoQueueDeadLetter(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)

    def tearDown(self):
        os.chdir(self._old_cwd)
        self._tmp.cleanup()
        sys.modules.pop("videoqueue", None)

    def _load_videoqueue(self):
        sys.modules.pop("videoqueue", None)
        import videoqueue  # noqa: PLC0415
        return importlib.reload(videoqueue)

    def test_dead_letter_and_bulk_retry(self):
        vq = self._load_videoqueue()

        self.assertTrue(vq.enqueue_pending("2026-02-01", "Ch", "Video A", "vid-a"))
        self.assertTrue(vq.enqueue_pending("2026-02-01", "Ch", "Video B", "vid-b"))

        for _ in range(3):
            vq.record_download_failure("vid-a", "network")
        for _ in range(3):
            vq.record_process_failure("vid-b", "ffmpeg")

        dead = vq.get_dead_letter(limit=50)
        ids = {d["video_id"] for d in dead}
        self.assertEqual(ids, {"vid-a", "vid-b"})

        by_status = {d["video_id"]: d["status"] for d in dead}
        self.assertEqual(by_status["vid-a"], "download_failed")
        self.assertEqual(by_status["vid-b"], "process_failed")

        results = vq.bulk_retry_failed(["vid-a", "vid-b"], actor="test")
        statuses = {r["video_id"]: r["new_status"] for r in results}
        self.assertEqual(statuses, {"vid-a": "pending", "vid-b": "processing"})

    def test_bulk_mark_deleted(self):
        vq = self._load_videoqueue()

        self.assertTrue(vq.enqueue_pending("2026-02-01", "Ch", "Video C", "vid-c"))
        out = vq.bulk_mark_deleted(["vid-c", "missing"], actor="test")
        result = {r["video_id"]: r["ok"] for r in out}
        self.assertTrue(result["vid-c"])
        self.assertFalse(result["missing"])


if __name__ == "__main__":
    unittest.main()
