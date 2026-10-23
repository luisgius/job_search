"""The reviewer's files reach the digest card: `main._attach_reviews` records
the paths that exist, and the card links them (and nothing that does not)."""

from __future__ import annotations

from pathlib import Path

from src import digest
from src.main import _attach_reviews
from src.models import Artifacts, ScoredJob
from src.reviewer import RESEARCH_FILE, REVIEW_FILE
from tests.conftest import NOW, make_job


def scored_with(folder: Path) -> ScoredJob:
    item = ScoredJob(job=make_job(company="Acme", title="Data Scientist"))
    item.artifacts = Artifacts(dir=str(folder), cv_md=str(folder / "cv.md"))
    return item


def test_existing_review_files_are_attached_and_missing_ones_are_not(tmp_path):
    reviewed, plain = tmp_path / "reviewed", tmp_path / "plain"
    reviewed.mkdir()
    plain.mkdir()
    (reviewed / REVIEW_FILE).write_text("# Review")
    (reviewed / RESEARCH_FILE).write_text("# Research")
    items = [scored_with(reviewed), scored_with(plain), ScoredJob(job=make_job())]
    _attach_reviews(items)
    assert items[0].artifacts.review_md == str(reviewed / REVIEW_FILE)
    assert items[0].artifacts.research_md == str(reviewed / RESEARCH_FILE)
    assert items[1].artifacts.review_md is None and items[1].artifacts.research_md is None


def test_the_card_links_the_review_relative_to_the_digest(tmp_path):
    folder = tmp_path / "applications" / "acme"
    folder.mkdir(parents=True)
    (folder / REVIEW_FILE).write_text("# Review")
    item = scored_with(folder)
    _attach_reviews([item])
    card = digest._item(item, digest_dir=tmp_path, now=NOW)
    assert card["review_md"] == "applications/acme/review.md"
    assert card["research_md"] is None
