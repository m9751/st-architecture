# st-architecture

**Role:** Internal architecture reference for the Smokin Stack — a single-page HTML doc covering the five-layer model, data flow, tooling map, and AI infrastructure inventory.

Live at: https://m9751.github.io/st-architecture/

**Current version:** v3.4 (Governance layer, App Portfolio, 45 hooks)

## What lives here

| Path | Purpose |
|---|---|
| `index.html` | Single-file deliverable — edit this to update the site |

## Site sections

| Section anchor | Content |
|---|---|
| `#overview` | Stack summary + version tag |
| `#layer-diagram` | 5-layer SVG architecture diagram |
| `#platform-tenant-model` | Platform vs tenant split |
| `#data-flow` | Signal → scoring → CRM data flow |
| `#app-portfolio` | Application inventory by layer |
| `#tables` | Reference tables (scoring engines, tooling) |
| `#scoring` | Scoring engine detail |
| `#ai-infra` | Enforcement hooks (45), specialist agents, MCPs |
| `#boundaries` | Scope limits |

## How to update

1. Pull latest: `git pull origin master`
2. Edit `index.html` — find the target section by its `id` (e.g. `id="layer-diagram"`)
3. Commit: `git commit -m "docs: [what changed] — vX.X"`
4. Open PR: `gh pr create --title "..." --body "..."`
5. Squash-merge: `gh pr merge --squash --delete-branch`
6. GitHub Pages auto-deploys within ~60 seconds

## Watch out for

| Situation | What happens | Fix |
|---|---|---|
| Local copy is behind origin | Edits conflict or overwrite shipped changes | Always `git pull origin master` before touching `index.html` |
| GitHub Pages branch | Site deploys from `master`, not `main` | PRs must target `master` |
| SVG layer diagram coordinates | Y-offsets are cumulative — adding a layer shifts everything below it | Adjust all downstream layer `y` values by the same delta as the new layer height |
| `feat/v3.4-rebuild` branch | Already merged — stale | Don't cherry-pick or rebase from it |

## Gaps (Hard Rule #1 — github-repo-foundations)

- [ ] `LICENSE` missing
- [ ] `.gitignore` missing

_Last updated: 2026-06-04 — v3.4_
