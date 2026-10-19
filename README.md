# job-watch

Checks the careers pages in `employers.yaml` every weekday morning (GitHub Actions) and lists job ads that are new since the last run.

- `data/latest.md` : today's new ads, plus any careers pages that stopped working
- `data/latest.json` : the same, for the Job Radar task
- `data/seen.json` : every ad seen so far, so nothing is reported twice

To add an employer, add an entry to `employers.yaml` (see the comments at the top). The workflow reruns automatically when that file changes.

Fit and knock-out checks are not done here. The Claude Job Radar task reads `data/latest.json` and does that.
