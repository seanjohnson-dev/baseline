# baseline

Stage-1 stub. When tests fail after a change, `baseline check` tells you, per failing test, whether the
change broke it (NEW), it was already broken (PRE-EXISTING / error changed), it is FLAKY, or it fails for
ENVIRONMENT reasons — with the evidence.

```
bin/baseline check [--base REF] [--runs N] [--json] [-- <test command>]
bin/baseline detect
python3 -m baseline version
```

Python 3.9+ standard library only. Runners: pytest, vitest, jest, go test, cargo test, rspec; anything
else gets a whole-command verdict. Decision rules: [docs/VERDICTS.md](docs/VERDICTS.md).
Tests: `python3 -m unittest discover -s tests` (or `pytest tests`).
