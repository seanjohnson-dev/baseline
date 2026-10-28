---
description: Attribute failing tests to this change (NEW) or not (PRE-EXISTING / FLAKY / ENVIRONMENT), with evidence
argument-hint: "[options] [-- <test command>]"
---

Run this from the repository root (or the project directory the tests run in):

```
"${CLAUDE_PLUGIN_ROOT}/bin/baseline" check $ARGUMENTS
```

If no test command was given and the runner can't be detected, ask which test command to use and pass it
after `--`. Then report the result to the user: first the summary line, then the NEW failures (they are
caused by the current change), then the other groups in one line each. Quote the base sha and error for
anything you call pre-existing. Do not describe a failure as pre-existing unless the report lists it under
PRE-EXISTING. Exit code 3 means the base result could not be established: say so.
