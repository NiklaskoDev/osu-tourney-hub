# NNN — hotfix: <title>

Status: draft | implemented
Version: X.Y.Z → X.Y.(Z+1)
Gate (fix only · no API/format change · no migration or new dependency · ≤ 3 source files · no critical decision): holds | fails → write a normal plan instead

Symptom: <what happens, where, since which version>
Repro: <steps or input>
Cause: <file:line — why it fails> (confirmed by: trace | code reading | failing test)
Fix: <file:line — the change; one line per file>
Regression test: <test file — case that fails before the fix and passes after>

Affected — changed symbols, their direct callers/readers, shared data they write:
| Part | Covered by |
|---|---|
| <path or symbol> | <test path> / add test / manual: <what to check> |

Decisions for the programmer: none | <question — options — recommendation>
Follow-ups: none | <cleanup or wider fix noticed, not done here>
