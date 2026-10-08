"""Tests for outreach scheduler jobs."""
import pytest


def test_outreach_replies_job_module_importable():
    """jobs.outreach_replies is importable and exposes outreach_replies_job."""
    from jobs.outreach_replies import outreach_replies_job
    assert callable(outreach_replies_job)
