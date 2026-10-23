"""Which missing skills cost the most jobs — the scorer's gaps, counted.

Adapted from ai-job-search's `/upskill` heatmap (MIT, Mads Lorentzen,
e16acee), without its model call: every scored job already carries the
scorer's `gaps` (stored since schema v7) and `reasons`, so the counting is
deterministic and free. Rows scored before v7 have reasons only; from those,
only the clauses that say something is missing ("does not mention", "no",
"lacks", …) are read.

A gap is a term from `VOCABULARY` found in that text **and absent from the
CV** — "CV lists AWS and Azure but not GCP" names three clouds and one gap.
Years-of-experience gaps skip the CV check: the CV saying "3 years" is
exactly what a "5+ years" gap is measured against.

The ranking puts near misses first: jobs scored within `NEAR_BAND` points
under the threshold, where one gap closed is the difference between the
digest and the bin. Rust in a job scored 15 is not a learning plan.

Run it with `python -m src.track gaps [--days N]`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

#: Points under the threshold that still count as a near miss.
NEAR_BAND = 15

#: (name, kind, pattern, check the CV). Case-insensitive unless the pattern
#: switches it off with `(?-i:…)` — "Go", "R" and "SAS" are words otherwise.
VOCABULARY: tuple[tuple[str, str, str, bool], ...] = (
    # Programming languages: rule 2's "major" tier when the role is built on one.
    ("Rust", "language", r"\brust\b", True),
    ("Scala", "language", r"\bscala\b", True),
    ("Java", "language", r"\bjava\b(?!\s*script)", True),
    ("Kotlin", "language", r"\bkotlin\b", True),
    ("C++", "language", r"(?<![\w+])c\+\+", True),
    ("C#", "language", r"(?<![\w#])c#", True),
    ("Go", "language", r"\bgolang\b|(?-i:\bGo\b)(?!-)", True),
    ("R", "language", r"(?-i:\bR\b)(?![&'])", True),
    ("SAS", "language", r"(?-i:\bSAS\b)", True),
    ("MATLAB", "language", r"\bmatlab\b", True),
    ("TypeScript/JavaScript", "language", r"\btypescript\b|\bjavascript\b|\bnode\.?js\b", True),
    # Spoken languages.
    ("Polish", "spoken", r"\bpolish\b", True),
    ("German", "spoken", r"\bgerman\b", True),
    ("Dutch", "spoken", r"\bdutch\b", True),
    ("French", "spoken", r"\bfrench\b", True),
    ("Italian", "spoken", r"\bitalian\b", True),
    ("Portuguese", "spoken", r"\bportuguese\b", True),
    ("Danish", "spoken", r"\bdanish\b", True),
    ("Swedish", "spoken", r"\bswedish\b", True),
    ("Norwegian", "spoken", r"\bnorwegian\b", True),
    ("Finnish", "spoken", r"\bfinnish\b", True),
    ("Czech", "spoken", r"\bczech\b(?!\s+republic)", True),
    # Cloud, data platforms, MLOps, BI.
    ("GCP", "tool", r"\bgcp\b|google cloud", True),
    ("AWS", "tool", r"\baws\b|amazon web services", True),
    ("Azure", "tool", r"\bazure\b", True),
    ("BigQuery", "tool", r"\bbig\s?query\b", True),
    ("Snowflake", "tool", r"\bsnowflake\b", True),
    ("Redshift", "tool", r"\bredshift\b", True),
    ("Databricks", "tool", r"\bdatabricks\b", True),
    ("Hive", "tool", r"\bhive\b", True),
    ("Trino/Presto", "tool", r"\btrino\b|\bpresto\b", True),
    ("Spark", "tool", r"\b(?:py)?spark\b", True),
    ("Kafka", "tool", r"\bkafka\b", True),
    ("Flink", "tool", r"\bflink\b", True),
    ("Hadoop", "tool", r"\bhadoop\b", True),
    ("dbt", "tool", r"\bdbt\b", True),
    ("Airflow", "tool", r"\bairflow\b|cloud composer", True),
    ("Dagster/Prefect", "tool", r"\bdagster\b|\bprefect\b", True),
    ("Kubernetes", "tool", r"\bkubernetes\b|\bk8s\b", True),
    ("Docker", "tool", r"\bdocker\b", True),
    ("Terraform", "tool", r"\bterraform\b", True),
    ("MLflow", "tool", r"\bmlflow\b", True),
    ("SageMaker", "tool", r"\bsagemaker\b", True),
    ("Vertex AI", "tool", r"\bvertex\s*ai\b", True),
    ("Kubeflow", "tool", r"\bkubeflow\b", True),
    ("CI/CD", "tool", r"\bci\s*/\s*cd\b", True),
    ("Tableau", "tool", r"\btableau\b", True),
    ("Looker", "tool", r"\blooker\b", True),
    ("Power BI", "tool", r"\bpower\s?bi\b", True),
    ("Elasticsearch", "tool", r"\belastic\s?search\b", True),
    # ML libraries and specialisms.
    ("TensorFlow/Keras", "ml", r"\btensorflow\b|\bkeras\b", True),
    ("JAX", "ml", r"\bjax\b", True),
    ("PyTorch", "ml", r"\bpytorch\b|\btorch\b", True),
    ("XGBoost/CatBoost", "ml", r"\bxgboost\b|\bcatboost\b", True),
    ("Hugging Face", "ml", r"hugging\s*face", True),
    ("LangChain/LlamaIndex", "ml", r"\blangchain\b|\bllama\s?index\b", True),
    ("Computer vision", "ml", r"computer vision|object detection|image (?:recognition|classification|segmentation)", True),
    ("NLP", "ml", r"\bnlp\b|natural language processing", True),
    ("LLM fine-tuning", "ml", r"fine[- ]?tun", True),
    ("Reinforcement learning", "ml", r"reinforcement learning", True),
    ("Recommender systems", "ml", r"recommend(?:er|ation)s?\s+(?:system|engine)", True),
    ("Deep learning", "ml", r"deep learning|neural network", True),
    ("Optimisation / OR", "ml", r"operations research|mathematical optimi[sz]ation|linear programming|\bsolvers?\b", True),
    ("MLOps", "ml", r"\bmlops\b", True),
    ("Bayesian methods", "ml", r"\bbayesian\b", True),
    ("Causal inference", "ml", r"causal inference|\bcausal\b", True),
    # Experience and degrees. Years skip the CV check (see module docstring).
    ("Years of experience", "experience",
     r"\b\d+\s*\+?\s*(?:-|to|–)?\s*\d*\s*\+?\s*years?\b|\byears of experience\b", False),
    ("PhD", "experience", r"\bph\.?\s?d\b|doctorate", True),
)

_COMPILED = tuple((name, kind, re.compile(pattern, re.I), cv_check)
                  for name, kind, pattern, cv_check in VOCABULARY)

#: A clause says something is missing.
_MISSING_RE = re.compile(
    r"\b(?:no|not|lacks?|lacking|missing|without|absent|never|none|neither|nor"
    r"|gaps?|limited|unclear)\b|n't\b",
    re.I,
)
_CLAUSE_SPLIT_RE = re.compile(r";|(?<=[.!?])\s+")


@dataclass
class Gap:
    name: str
    kind: str
    minor: bool = False
    jobs: list[dict[str, Any]] = field(default_factory=list)
    near_misses: int = 0

    @property
    def average(self) -> int:
        return round(sum(job["score"] for job in self.jobs) / len(self.jobs)) \
            if self.jobs else 0


@dataclass
class GapReport:
    since: datetime
    threshold: int
    scored: int
    unrecognised: int
    gaps: list[Gap]


def missing_clauses(reasons: Iterable[str]) -> list[str]:
    """The clauses of free-text reasons that say something is missing."""
    clauses = []
    for reason in reasons:
        for clause in _CLAUSE_SPLIT_RE.split(str(reason)):
            if _MISSING_RE.search(clause):
                clauses.append(clause.strip())
    return clauses


def terms_in(texts: Iterable[str], cv_text: str) -> set[str]:
    """Vocabulary terms named in `texts` that the CV does not show."""
    joined = "\n".join(str(text) for text in texts)
    found = set()
    for name, _kind, pattern, cv_check in _COMPILED:
        if pattern.search(joined) and not (cv_check and pattern.search(cv_text)):
            found.add(name)
    return found


def _minor_names(minor_gaps: Iterable[Any]) -> set[str]:
    """Vocabulary terms the config's `scoring.minor_gaps` lists as siblings of
    something in the CV."""
    joined = "\n".join(str(line) for line in minor_gaps or [])
    return {name for name, _kind, pattern, _cv in _COMPILED if pattern.search(joined)}


def build_report(rows: Iterable[Mapping[str, Any]], *, cv_text: str, threshold: int,
                 since: datetime, minor_gaps: Iterable[Any] = ()) -> GapReport:
    kinds = {name: kind for name, kind, _pattern, _cv in VOCABULARY}
    minor = _minor_names(minor_gaps)
    gaps: dict[str, Gap] = {}
    scored = unrecognised = 0
    for row in rows:
        score = row.get("score")
        if not isinstance(score, int) or isinstance(score, bool):
            continue
        scored += 1
        stated = row.get("gaps") or missing_clauses(row.get("reasons") or [])
        names = terms_in(stated, cv_text)
        if not names:
            unrecognised += bool(stated)
            continue
        job = {"score": score, "company": str(row.get("company") or ""),
               "title": str(row.get("title") or "")}
        near = threshold - NEAR_BAND <= score < threshold
        for name in names:
            gap = gaps.setdefault(name, Gap(name, kinds[name], name in minor))
            gap.jobs.append(job)
            gap.near_misses += near
    ranked = sorted(gaps.values(),
                    key=lambda gap: (-gap.near_misses, -len(gap.jobs), gap.name))
    return GapReport(since, threshold, scored, unrecognised, ranked)


def report_lines(report: GapReport, *, top: int = 25, examples: int = 3) -> list[str]:
    head = (f"Skill gaps in {report.scored} scored job(s) since "
            f"{report.since.date().isoformat()} (threshold {report.threshold})")
    if not report.gaps:
        return [head, "", "No gap from the vocabulary was found."]
    lines = [head, "",
             f"{'skill':<24} {'kind':<17} {'jobs':>4}  {'near misses':>11}  {'avg score':>9}"]
    for gap in report.gaps[:top]:
        kind = gap.kind + (" (minor)" if gap.minor else "")
        lines.append(f"{gap.name:<24} {kind:<17} {len(gap.jobs):>4}  "
                     f"{gap.near_misses:>11}  {gap.average:>9}")
    low = report.threshold - NEAR_BAND
    lines += ["",
              f"Near misses are jobs scored {low}-{report.threshold - 1}: one gap "
              "closed there is the difference between the digest and the bin.",
              "(minor) = listed in scoring.minor_gaps, so it already costs only a "
              "few points."]
    if report.unrecognised:
        lines.append(f"{report.unrecognised} job(s) had gaps no vocabulary term "
                     "matched (src/skill_gaps.py VOCABULARY).")
    lines += ["", "Behind the top gaps:"]
    for gap in report.gaps[:examples]:
        jobs = sorted(gap.jobs, key=lambda job: -job["score"])[:4]
        lines.append(f"  {gap.name}: " + "; ".join(
            f"{job['company']} - {job['title']} ({job['score']})" for job in jobs))
    return lines


def gap_report(tracker: Any, config: Any, *, days: int, now: datetime) -> GapReport:
    """Read the tracker and the CV, and count."""
    since = now - timedelta(days=days)
    cv_text = ""
    try:
        cv_text = config.cv_path.read_text(encoding="utf-8")
    except (OSError, AttributeError):
        pass
    threshold = int(config.get("scoring.threshold", 65) or 65)
    return build_report(
        tracker.scored_applications(since.isoformat()),
        cv_text=cv_text, threshold=threshold, since=since,
        minor_gaps=config.get("scoring.minor_gaps", []) or [],
    )
