# PikBot prediction review and prospective accuracy

## Current status

`team-review-v1` is research-only. No 75% accuracy claim is validated and no pick receives recommendation approval. A team of deterministic statistical reviewers shares evidence; these are not separate trained AI experts or independent probability estimates. No paid LLM service was added.

Paid users can browse available research props and the review for each selected player. Uncalibrated card percentages were replaced by Research / Not approved. Detailed model gauges are explicitly raw scores out of 100. Trial selections are withheld rather than forced while approval prerequisites are missing, including already-saved boards issued by the earlier model. Existing billing and owner permissions are unchanged.

## Reviewers

- Data: source observation no older than 15 minutes and scheduled start still in future.
- Projection: at least 20 historical games and baseline score at least 0.75. This threshold is a research screen, not proof of 75% accuracy.
- Trend: at least seven of ten recent results on the proposed side of the current line. Retrospective hit frequency is not a forward probability.
- Playing time: stable recent minutes with a projected minimum of 15. BDL and Dunk minutes are now retained when supplied. This is a basketball heuristic; other sports need dedicated opportunity inputs and fail this check when those are absent.
- Sport-specific availability: identifies missing lineups/injuries and league-specific context. It does not manufacture those feeds.
- Market pricing: optional Pinnacle exact-line reference via the existing Odds API entitlement; two valid sides, synchronized observations within five minutes and neither older than 15 minutes. It uses proportional devig, labels the result as market-implied, and never treats one-sided odds as true odds. Provider coverage may return no benchmark player props. This adds one cached reference query per viewed matchup/market set and consumes existing provider credits; it does not purchase a plan.
- Validation: blocks recommendation approval until a defensible prospective evaluation and calibration are implemented and pass review. It cannot be unlocked by merely raising a displayed score.

## Prospective ledger and owner report

Before returning a new research board, PikBot inserts a private, account-free record with issue timestamp, game, player, exact source/line/side, model version, raw score, source observation, history fingerprint and review evidence. The first observation of a distinct model/event/source/player/stat/line/side is immutable and repeated views are not additional predictions. Same-line revisions are not a new performance trial. If recording fails or game time passes, the endpoint withholds the board.

Owner > Accuracy & specialist team summarizes all new ledger records, with wins/losses, pending, pushes, voids, Brier score, score ranges, sport and version. It starts with this release; do not mix it with the user's manually reported 25–20 record. Historical lines, opposite sides, providers and players in a game are correlated, so an independence-based Wilson interval can be too narrow. No public performance claim is automatically authorized by this report.

Owner's Check recorded predictions button grades batches of eight due records against final ESPN player box scores, up to 400 per click. A 30-minute lease prevents repeat unresolved lookups; pending records never become losses merely due to a timeout. This is owner-triggered, not an unattended scheduled worker. DNPs without final player stats remain pending for review; sportsbook-specific void rules are not assumed. Old Previous Pick Results continues showing its existing rolling 48-hour view. The prospective aggregate audit is separate and persists to support longitudinal validation.

## Live performance

Paid members and the owner can tap a started matchup for observed scores and player stats. The ESPN snapshot is cached for 30 seconds and manually refreshed; it may be delayed. The view covers the supported stat mappings for all seven leagues when the source supplies a unique matching game. These are game facts, not executable in-play picks. No live probability model is enabled. Reliable live recommendations additionally require synchronized current lines, remaining time, player availability, sport-specific opportunity models, settlement rules and separate prospective validation.

## Backtest corrections

Date-only game records on the offer day are excluded, exact duplicate date/value rows do not inflate sample size, and conflicting same-date values are rejected. A single strongest side is chosen before outcomes are inspected. Two-pick entry grading looks up the actual selected side rather than the first side of the offer. NBA combination and playing-time feature logic now runs in the uploaded historical backtest too. Uploaded tests remain research-only and do not validate live deployment, news features, selection rules or chronological held-out accuracy.

## Expected value

The owner calculator requires explicit probabilities and the actual entry's total-return payout table. It enumerates the independent Bernoulli distribution and reports expected net return per unit. No universal platform payout defaults, automatic bankroll advice, DFS single-leg EV, correlation bonus, CLV, or guaranteed-profit label is invented. Conditional market estimates and a probability-point difference are not interchangeable with whole-entry EV. Same-game correlation, pushes, voids, variable multipliers and differing settlement rules need separate treatment.

## Validation performed

Tests use synthetic fixtures and mocked providers/database connections. They verify software correctness, PostgreSQL SQL parsing, permission boundaries, before-game recording, math and unavailable-data behavior. Passing these tests is not a betting win-rate result. Real performance must be measured on untouched, timestamped prospective predictions after games finish. Do not tune thresholds on the same results used to report accuracy.


## Owner offline settled-pick diagnostic

Run `python accuracy_audit.py settled_picks.csv` against an owner-exported,
timestamped, prospective set of settled predictions. Required columns are
`issued_at,event_id,sport,player,stat,line,side,actual,p_raw`.
Optional grouping columns: `platform,model_version`.
The script deduplicates repeated same-line selections, separates pushes,
reports Brier score and compares observed hit rates with raw estimates by
confidence bucket, sport and stat. It never alters the live prediction
engine, subscriptions, or published boards.

Before trusting results, verify that picks were logged before game start,
that grading follows each platform's rules, and that the dataset contains
all issued predictions rather than selected winners. Do not advertise a
75% win rate without sufficient prospective evidence. A performance audit
is a prerequisite to selecting and validating a candidate model, not itself
a model upgrade.
