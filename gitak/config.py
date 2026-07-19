"""Central configuration for Gitak.

Armenian school conventions, all tunable:
  - 10-point grading scale (integers on individual exams)
  - 4 quarters per school year
  - 12 grade levels
A school year is stored as its starting calendar year: 2025 means 2025-26.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "school.db"

GRADE_MIN = 1
GRADE_MAX = 10

# Armenian scale: 1-3 unsatisfactory, 4-5 satisfactory, 6-7 good, 8-10 excellent.
PASS_THRESHOLD = 4.0
# Below this predicted quarter average a student is flagged as needing support.
WEAK_THRESHOLD = 6.0
# Below this the flag is high risk (close to failing).
HIGH_RISK_THRESHOLD = 5.0

QUARTERS = (1, 2, 3, 4)
GRADE_LEVELS = range(1, 13)

# Peer tutoring: minimum quarter average to qualify as a tutor,
# and the most tutees one tutor takes per subject.
TUTOR_MIN_AVG = 8.5
TUTOR_MAX_LOAD = 2

# Gitak Score weights (score is 0-1000 per quarter).
SCORE_LEVEL_WEIGHT = 600      # absolute performance
SCORE_IMPROVE_WEIGHT = 250    # progress vs previous quarter (125 = holding steady)
SCORE_CONSISTENCY_WEIGHT = 150  # low spread across subjects

# End-of-year support program: flag students with a year average below
# WEAK_THRESHOLD in at least this many subjects.
SUPPORT_MIN_SUBJECTS = 3

DEFAULT_PORT = 3303


def timeline_index(year: int, quarter: int) -> int:
    """Monotonic index over (school_year, quarter) so cross-year transitions
    (Q4 of one year -> Q1 of the next) are ordinary consecutive steps."""
    return year * 4 + (quarter - 1)


def from_timeline_index(t: int) -> tuple[int, int]:
    return t // 4, t % 4 + 1
