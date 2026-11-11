# AI Disclosure

**Status: draft.** This draft was written by Claude Code at the author's request on
2026-11-10 from what the assistant can see: the git history and the working session in
which it did the work described in section 2. It does not cover anything the assistant
did not see. The author must read it, correct it, and complete the section marked
"TO BE COMPLETED BY THE AUTHOR". Nothing here has been checked against a course policy.

## 1. Tools

| Tool | Where it is evidenced |
|---|---|
| Claude Code (Anthropic's coding assistant), used inside VS Code | The working session of 2026-11-10; the assistant itself runs there |
| Claude Sonnet 5.5 | Named in the `Co-Authored-By` trailer of three commits (below). The assistant also ran as Claude Opus 5.5 earlier in the same session before the model was switched; that is known from the session, not from the repository |

Commits that carry an AI co-author trailer (3 of 82 commits):

| Commit | Date | Subject |
|---|---|---|
| `b646b1e` | 2026-11-10 | Add explicit evidence-based recovery for retraining requests left running |
| `d5c3dc9` | 2026-11-10 | Add coverage tests for the gateway server entry point |
| `90363c4` | 2026-11-10 | Record unit-test coverage evidence for backend and src/fraudshield |

The other 79 commits (2026-10-18 to 2026-11-07) have no AI trailer. A missing trailer does
not show that no AI was used; see section 3.

## 2. How Claude Code was used in the session of 2026-11-10

The author gave written instructions with explicit rules (for example: never open `.env` or
the gateway admin key, do not run training or the worker, do not push, leave changes for the
author to commit). The assistant carried out the following. "Committed" means the author
committed it with the trailer above; everything else was left uncommitted for the author.

1. **Installed a supplied patch.** The author supplied `FraudShield_recovery_patch.zip`. The
   assistant checked its SHA-256 and file checksums, ran the installer's check and install
   modes, and ran the new tests (31 tests). It did not write that patch. Committed (`b646b1e`).
2. **Ran and reported CI.** It ran `scripts/run_ci.py` several times and reported the real
   outputs, including one intermittent failure of an existing test that it did not fix.
3. **Read-only audits.** It audited the repository against the author's development guide and
   the course rubric, and prepared a GitHub submission proposal. Reports were written outside
   the repository.
4. **Tests and coverage.** It wrote `tests/test_gateway_server_coverage.py` (10 tests), measured
   coverage with `coverage` (twice), and recorded it in `reports/coverage_summary.txt` and
   `requirements-dev.txt`. Committed (`d5c3dc9`, `90363c4`); the later re-measurement is
   uncommitted.
5. **Source bundles.** At the author's request it prepared text bundles of source files so the
   author could share them with an assistant for later work.
6. **Code and documentation changes made after those commits (uncommitted at writing).**
   - Logging: `backend/logging_config.py` and log calls in the API, gateway, alert receiver
     and retraining worker.
   - One-line docstrings for every class and function in `backend/` and `src/fraudshield/`,
     and style fixes found by `pycodestyle`.
   - A DVC stage `unit_tests` with `scripts/run_unit_tests.py`, `.dvcignore` lines, and the
     gate report declared as a DVC metric.
   - Frontend changes to `frontend/index.html` and `frontend/pipeline.html`.
   - `scripts/log_feature_importance.py` and `scripts/promote_model.py`.
   - Grafana dashboard JSON fixes.
   - A DVC remote entry in `.dvc/config` (a relative local folder; no credentials).
   - `README.md`, `docs/user_manual.md`, `docs/operator_guide.md`, `docs/test_plan.md`,
     `docs/test_report.md`, `docs/low_level_design.md`, `docs/high_level_design.md`,
     `docs/dataset.md`, `docs/openapi.json` and this file.
   - New tests for all of the above: 129 tests in 15 files added after the 110-test run
     (see `docs/test_plan.md`).

What the assistant deliberately did not do: it did not retrain or change any model (the
deployed model file kept the SHA-256 `1d2df493...`), did not run the retraining worker, did
not open `.env` or `runtime/gateway-admin.key`, did not push, and did not run the real
`scripts/promote_model.py` or `scripts/log_feature_importance.py` against the project's
MLflow database. It stopped on one requested change (`params:` in `dvc.yaml`) because it
would have re-run training.

## 3. TO BE COMPLETED BY THE AUTHOR

The assistant cannot see the following. Please fill in each item; leave "none" where it is true.

- **Earlier AI use (before 2026-11-10).** Which assistants or tools did you use for the
  code, tests, documents and reports committed between 2026-10-18 and 2026-11-07? For which
  files? The three files supplied from outside this session
  (`FraudShield_recovery_patch.zip`, `FraudShield_Recovery_Source.txt`,
  `FraudShield_Development_Guide.pdf`): where did they come from and who wrote them?
- **What you wrote or changed yourself.** List the parts you wrote, designed or rewrote
  personally (for example the problem choice, the design decisions, specific modules, the
  report text), and what you changed in AI-produced material.
- **How you checked AI output.** What you read, ran or tested yourself, beyond the CI runs
  and tests reported above.
- **Prompts and instructions you kept.** If the course asks for the prompts used, where they
  are stored.
- **Course policy.** Which course rule on AI use applies, and whether this disclosure meets
  it.
- **Corrections to sections 1 and 2.** Anything the assistant stated that is wrong or
  incomplete.

## 4. Impact (facts from the repository)

- **Tests.** 110 tests in the last run before 2026-11-07 (commit `187fb3a`); 239 tests at
  the time of writing. Coverage of `backend/` and `src/fraudshield/` was measured at 93%
  (`reports/coverage_summary.txt`).
- **Models and results.** No model was retrained and no recorded metric was changed by the
  assistant's work.
- **Behaviour changes.** Logging was added to services (no API request or response contract
  changed); the web pages gained inline field errors, plain-language labels and an address
  memory on the pipeline page; the CI gained one DVC stage that runs the unit tests.
- **Not verified.** The frontend changes were not run in a browser; container images were not
  rebuilt; the manual rollback procedure, `dvc push` and `dvc pull`, and a fresh-clone setup
  were not run; Grafana was not opened.
- **Known limits of AI-written material here.** Documents describe what the code and reports
  show; statements that could not be checked are marked "not verified" in the documents.
  One existing test fails intermittently under load (see `docs/test_report.md`).
