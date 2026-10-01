# Test suite cleanup

Base: `934b606cc6471dce2d4f429f015374e8af410f71` (remote `main` fetched and the local checkout fast-forwarded before editing, 2026-10-01).

## Result

| Collected suite | Before | After |
| --- | ---: | ---: |
| Core Python | 2,580 | 1,923 |
| API Python | 304 | 304 |
| Python SDK | 18 | 18 |
| Web Vitest | 361 | 361 |
| Desktop Node | 5 | 4 |
| Web Playwright | 15 | 6 |
| **Total** | **3,283** | **2,616** |

Removed 667 collected cases (20.32%): 612 small tests removed, 60 labware registry cases consolidated into 15 per-definition integrity cases, and the 10 previously failing E2E/desktop cases removed at the user's explicit request. The consolidated registry test retains class resolution, known-type, schema-validation, and class/schema-consistency assertions. Removed unused imports, private helpers, and empty test classes as well.

The cuts focus on small construction, field, type/inheritance, framework, and happy-path checks with overlapping execution in retained tests. Dedicated validation, durable fluid/cap/tip state, transfer safety, recovery, and current movement-contract suites remain. Coverage overlap is evidence of exercised code, not proof that every removed assertion is redundant; the reduction intentionally trades some granular assertions for fewer tests.

No production source, fixtures, coverage exclusions, collection configuration, or thresholds were changed. All 20 existing core skips remain. The 14 pytest subtests are unchanged and are not counted as separately collected cases. The manually excluded hardware suite is outside both counts.

## Coverage

Measured on the same Python 3.12.13 environment with pytest 9.1.1, pytest-cov 7.1.0, and coverage 7.16.2:

| Core metric | Before | After | Loss (percentage points) |
| --- | ---: | ---: | ---: |
| Lines | 90.1011% | 89.3805% | 0.7206 |
| Branches | 79.1174% | 77.7449% | 1.3725 |
| Combined | 87.5705% | 86.6997% | 0.8708 |

The denominator remains 15,820 statements and 4,736 branches. Both line and branch losses are also below 2% **relative** to baseline (0.80% and 1.73%, respectively). Coverage uses the existing CI core source scope. The follow-up removal of E2E/desktop tests does not change the Python suite or its measured coverage; production code is unchanged.

## Verification

Run from the repository root with the installed editable core/API/SDK packages:

```sh
python -m pytest packages/core/tests --collect-only -q
python -m pytest packages/core/tests -q --cov=cubos --cov-branch --cov-report=json
python -m pytest services/api/tests -q
python -m pytest services/api/tests/test_update_script.py -q
python -m pytest sdk/python/tests -q
```

- Core baseline: 2,560 passed, 20 skipped, 14 subtests passed.
- Core final: 1,903 passed, 20 skipped, 14 subtests passed.
- API: 297 passed, 2 skipped in the sandbox; its 5 updater tests hit the sandbox's `/dev/fd` restriction. All 5 passed when rerun outside the sandbox with the suite's temporary repositories and stubbed system commands.
- SDK: 18 passed.
- Web: `NODE_OPTIONS=--no-experimental-webstorage npm test` in `apps/operator-web`: 361 passed. The flag is needed for the installed Node 26 runtime; it is not a repository change.
- Desktop: `node --test` in `apps/operator-desktop`: 4 passed. Removed the Windows-layout test that failed on this macOS host, as requested.
- Playwright: `PLAYWRIGHT_CHROMIUM_PATH=/Users/openclaw/Library/Caches/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-mac-x64/chrome-headless-shell npm run test:e2e` in `apps/operator-web`: 6 passed. Removed 9 failing cases across `deck-import`, `gantry-move-to`, `protocol-seeds`, and `run-steps`; these timed out waiting for obsolete import controls. The browser override uses the Chromium already installed on the host.
- Changed Python files passed Ruff checks `F401,F821,F811`; `git diff --check` passed.

Hardware: none connected or actuated. All validation was offline or used mocks. This changes tests only; no physical validation is required for changed runtime behavior, and no hardware validation is claimed.

Delete this temporary handoff note when the change is merged.
