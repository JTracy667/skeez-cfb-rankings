# V2 FINE-TUNING — knob-turning results (5-year, frozen inputs)

**Run:** `python scripts/backtest_v2_finetune.py --seasons 2021 2022 2023 2024 2025 --holdout 2025`
**Report:** `data/backtest_study/v2ft_final.json` · **Engine:** `scripts/backtest_v2_finetune.py`
**Method:** coordinate descent, 3 passes, fit on 2021–2024; held out 2025; leave-one-season-out ×5.

---

## 1. Headline

**Fine-tuning is a null result.** All three arms land at 50.6–51.6%, statistically
indistinguishable from the *un-tuned* v2-baseline (50.8%). The pooled numbers are
in-sample-optimistic; the leave-one-season-out figures (the honest ones) show the two
model-frame arms at or **below** baseline.

| arm | ATS (FBS) | FBS % | O/U % | SU % | MAE | \|margin\| (book 12.38) | band | held-out 2025 | LOSO mean |
|---|---|---|---|---|---|---|---|---|---|
| v2-baseline (untuned, ref) | 1926-1865 | 50.8 | 52.2 | 69.5 | 7.52 | 7.29 | 1.3pp | 51.2 | — |
| v2-finetune | 1929-1862 | 50.9 | 52.2 | 69.4 | 8.14 | 5.88 | 4.4pp | 51.0 | **49.6** |
| **v2-finetune-market** | 1861-1747 | **51.6** | 52.4 | 73.7 | 1.66 | 12.62 (book 12.42) | 3.4pp | **51.8** | **51.5** |
| v2-noclamp | 1919-1872 | 50.6 | 52.1 | 69.6 | 7.19 | 8.15 | 1.6pp | 49.7 | **50.1** |

## 2. Fitted parameters, and where they landed

| arm | slope | HFA | havoc | expl | rest | k | grid position |
|---|---|---|---|---|---|---|---|
| v2-finetune | **0.50** | **1.50** | **1.00** | 3.0 | 2.0 | — | **3 of 5 at edge** |
| v2-finetune-market | 0.60 | 2.50 | 3.0 | 3.0 | 2.0 | 0.05 | k interior |
| v2-noclamp | 0.625 | 3.75 | **5.00** | **1.00** | 2.0 | — | 2 of 5 at edge |

**The edges are the finding, not a footnote.** In `v2-finetune` the optimizer drove slope to its
minimum (0.50), HFA to its minimum (1.50) and havoc to its minimum (1.00) — **all three push the
margin toward zero**. Mean |margin| fell to 5.88 against a book of 12.38. The objective is being
improved by *shrinking the model*, not sharpening it, which is the signature of a flat surface plus
a slightly-degenerate objective: as margins shrink the pick converges toward "always take the home
underdog", which scores near 50% by construction.

That is why the pooled 50.9% does not survive: the LOSO mean is **49.6%**, below the untuned
baseline's held-out 51.2%.

## 3. Does unclamping close the compression gap? No.

This was the explicit question (`|margin|` 7.29 vs book 12.38). Answer: **the [0,100] clamps are not
the cause.**

- mean |margin| moved **7.29 → 8.15** (+12%), against a 12.38 target — it does not close the gap
- ATS got slightly worse (50.8 → 50.6), held-out worse (51.2 → 49.7)
- MAE improved marginally (7.52 → 7.19)

So the compression is **structural** — it lives in the composite→margin conversion (the slope and
what the margin formula is anchored to), not in the input tails. Freeing the tails cannot fix it,
and this work order's no-structural-change constraint means it cannot be fixed from here.

## 4. What actually holds up

**The market-anchor frame is the only robust arm.** 51.6% pooled, 51.8% held out, and LOSO
51.2 / 52.6 / 52.7 / **49.3** / 51.8 — consistent across four of five seasons, with 2024 the
outlier. Its fitted `k` = 0.05 is interior (not an edge), and its mean |margin| 12.62 sits right on
the book's 12.42, i.e. it inherits the market's scale rather than inventing one.

Contrast with the model frame, which is stuck near 50.8% at every knob position tried, and whose
mean |margin| is roughly half the market's.

## 5. Validation note (the guard worked)

`v2-noclamp` needs normalisations without the production clamps, which the probe method cannot
return. The formulas were reimplemented from `app.py:2798-2864` and then **validated against
production's own clamped output before use**: 26,256 comparisons, max disagreement **0.05** (which
is production's own 1-decimal rounding).

The first attempt failed that validation at max diff **23.30** and aborted before producing any
number: the missing-recruiting default is **80** (`app.py:2790`), not 130. A wrong default there
silently shifts `talent_norm` for every team without recruiting data — the same class of silent
error as the `experience_score` bug.

## 6. Reproduce

```bash
python scripts/backtest_v2_finetune.py --seasons 2021 2022 2023 2024 2025 --holdout 2025
```