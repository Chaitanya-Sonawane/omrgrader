"""
Real-sheet validation for the OMR engine.

Runs `scan_omr_sheet` over every image in `SAMPLESSHEET/` and reports, per
image: how many of the 160 bubbles were detected, the per-question answers,
and how many questions were flagged for teacher review (low confidence /
multiple marks / not detected).

It then measures ANSWER STABILITY across the near-duplicate frames (several
photos of the same physical sheet): for each question it takes the majority
answer across all frames and reports the % of (frame, question) reads that
agree with that majority. A high agreement means the scanner reads the same
sheet consistently regardless of the exact photo.

Run:  python test_sample_sheets.py
"""
import glob
import os
from collections import Counter

from omr_engine import scan_omr_sheet

SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "..", "SAMPLESSHEET")


def main():
    files = sorted(glob.glob(os.path.join(SAMPLE_DIR, "*.jpeg")))
    if not files:
        print("No sample sheets found in", SAMPLE_DIR)
        return

    per_frame_answers = []
    print(f"Scanning {len(files)} sample sheet(s)\n")
    for f in files:
        try:
            r = scan_omr_sheet(f)
        except Exception as e:  # noqa: BLE001
            print(f"  {os.path.basename(f)[:34]:34}  ERROR: {e}")
            continue
        ans = {q: b.selected for q, b in r.answers.items()}
        flagged = sum(1 for b in r.answers.values() if b.flagged)
        reasons = Counter(b.flag_reason for b in r.answers.values() if b.flagged)
        per_frame_answers.append(ans)
        print(f"  {os.path.basename(f)[:34]:34}  "
              f"detected {r.quality['bubbles_detected']:3}/160  "
              f"flagged {flagged:2}  {dict(reasons)}")

    # ---- pairwise similarity between frames ----
    # NOTE: the folder holds photos of DIFFERENT students' sheets, so a global
    # "majority answer" is not meaningful. Instead we report, for every pair of
    # frames, how many of the 40 answers coincide. Pairs that are actually the
    # SAME physical sheet photographed twice should score near 100% (that is the
    # real read-consistency signal); pairs of different sheets will score low.
    n = len(per_frame_answers)
    if n >= 2:
        best_pairs = []
        for i in range(n):
            for j in range(i + 1, n):
                a, b = per_frame_answers[i], per_frame_answers[j]
                qs = sorted(set(a) | set(b))
                same = sum(1 for q in qs if a.get(q) == b.get(q))
                best_pairs.append((100.0 * same / len(qs), i, j))
        best_pairs.sort(reverse=True)
        print("\nMost-similar frame pairs (candidate duplicates of one sheet):")
        for pct, i, j in best_pairs[:3]:
            print(f"  frame {i} vs frame {j}: {pct:.1f}% identical answers")


if __name__ == "__main__":
    main()
