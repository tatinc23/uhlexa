# Submission checklist: Build, Ship, Shape (Alexa+ track)

**Deadline: Friday Oct 23 2026, 12:00 PM Pacific.** Devpost locks the entry after that. Save a draft early; drafts can
be edited until the deadline. Rules read live 2026-10-22 from https://amazonappdev2026.devpost.com/rules.

## What the rules require, and where we stand

| Requirement (official rules) | Status | Where |
|---|---|---|
| Alexa+: self-hosted MCP server, spec 2025-11-25 or later, Streamable HTTP, **or** a simulated Alexa+ experience | We do both. MCP SDK 2.3.0 negotiates 2025-11-25 and later. The MCP server (Task 7) and simulator (Task 8) are being built now | `app/mcp/`, `web/simulator/`, `/voice/*` |
| GitHub repo URL, **public with an open-source license visible in the About box**, or private and shared | Plan: a clean public copy `tatinc23/uhlexa`, one commit, MIT | `LICENSE`, `scripts/make_public_copy.sh` |
| Repo has clear setup and run instructions | Drafted. Re-run every command from a fresh clone before submitting | `README.md` |
| Text description of features and functionality | Drafted; claims checked against the final tree 2026-10-23 | `docs/submission/DEVPOST_TEXT.md` |
| Video **under 3:00**, public on YouTube or Vimeo, shows the project working, **no third-party trademarks or copyrighted music without permission** | Script drafted (≤ 2:50) | `docs/submission/VIDEO_SCRIPT.md` |
| **Product Feedback for each tool/API/SDK** (the form has 5 combined questions, Q1-Q5) | Drafted, 12 tools; KEN CONFIRMS the "build again" answers | `DEVPOST_FORM.md` §16-20 |
| Track: Alexa+ · Mini challenges | Alexa+. AWS Builder: No (no AWS service; the form still wants an N/A line). Open Source: No for now, KEN DECIDES (option: a separate MIT Keiki gateway client) | `DEVPOST_FORM.md` §7, §10, §11 |
| Optional friction log (**up to 10% bonus**) and feature requests | Drafted: 15 entries, 6 requests | `docs/submission/FRICTION_LOG.md` |
| Testing instructions; credentials if the site is private | Judges may judge from video and text alone. The public URL is gated, so say so and give local-run steps | `DEVPOST_TEXT.md` → Testing |
| Third-party tools used under their terms | Every tool used under its current terms; all named publicly (Ken's decision 2026-10-22); no vendor logos in the video | `docs/research/` |
| English | Yes | — |

## Ken: click-by-click

### 1. Create the empty public repo (2 min, free)
1. Go to https://github.com/organizations/tatinc23/repositories/new.
2. Repository name: `uhlexa`. Visibility: **Public**.
3. Leave **Add a README**, **.gitignore** and **license** all unchecked (the copy brings its own).
4. Click **Create repository**.

### 2. Build and push the clean copy (after the build sessions finish and push)
Run from any checkout of `alexa-hackathon` (replace the ref if the final branch differs):

```bash
git fetch origin claude/uhlexa-hackathon-demo-f67b12
```

```bash
scripts/make_public_copy.sh FETCH_HEAD ~/Github/uhlexa-public https://github.com/tatinc23/uhlexa.git
```

```bash
git -C ~/Github/uhlexa-public push -u origin main
```

The script drops internal notes (`TASKS.md`, `BLOCKERS.md`, `DECISIONS.md`, handoffs, prompts, `.claude/`), replaces
password-manager item ids, the vault name, local paths and the demo iMessage number, adds `LICENSE`, and stops if
gitleaks finds anything outside the tests. It never pushes by itself.

3. On https://github.com/tatinc23/uhlexa, check that the right-hand **About** box shows **MIT license** (GitHub detects
   `LICENSE` automatically). If not: click the gear next to About and save once.

### 3. Video
1. Record per `VIDEO_SCRIPT.md` (Mac running `scripts/run_public.sh` and `scripts/voice_worker.sh`).
2. No vendor logos or dashboards on screen, no commercial music, under 3:00.
3. Upload to YouTube with visibility **Public** (the rules say "publicly visible"; unlisted is a risk). Copy the link.

### 4. Devpost form (https://amazonappdev2026.devpost.com → **Enter a submission**)
All field text is in one file, in form order: `docs/submission/DEVPOST_FORM.md` (24 fields). Paste from it.

| # | Devpost field | Paste from `DEVPOST_FORM.md` | Ken |
|---|---|---|---|
| 1 | About the project | Section 1 (Markdown) | Cut any bullet the recording does not show |
| 2 | Built with (up to 25 tags) | Section 2 | Confirm vendor names are OK |
| 3 | Try it out links | Section 3 | |
| 4 | Image gallery (3:2, 5 MB max) | Section 4 capture list | Take screenshots |
| 5 | Upload a file (optional) | Section 5 | Optional |
| 6 | Submitter type, organization, country, province | Section 6 | KEN DECIDES |
| 7 | Primary track | Alexa+ | |
| 8 | GitHub repo URL (public + license, or shared with testing@devpost.com and @AmazonAppDev) | `https://github.com/tatinc23/uhlexa` | |
| 9 | New or existing before Aug 31 2026 | New | |
| 10 | AWS Builder mini challenge | No (required text box: N/A line) | |
| 11 | Open Source mini challenge | No for now | KEN DECIDES |
| 12 | Feature requests (optional) | Section 12 | |
| 13 | Friction log (a URL) | `.../blob/main/docs/submission/FRICTION_LOG.md` | Check URL loads after push |
| 14 | Project testing link (optional) | Leave empty | |
| 15 | Testing instructions or credentials | Section 15 | Paste the access link with the real token (Ken: live URL stays up Nov 9-20; docs/HOSTING.md judging section) |
| 16-20 | Feedback Q1-Q5 | Sections 16-20 | KEN CONFIRMS Q5 |
| 21 | Eligibility checkboxes | Section 21 | KEN CONFIRMS, tick yourself |
| 22 | AI tools used | Section 22 | KEN ADDS other tools |
| 23 | Level of learning, AI value for career | Section 23 | KEN DECIDES |
| 24 | Video URL (public YouTube/Vimeo) | Section 24 | Upload video |

Click **Save draft** early, then **Submit** before 12:00 PM PT. Devpost shows the entry as submitted on your dashboard.

## Suggested timeline (Friday)
| By (PT) | Done |
|---|---|
| 08:00 | Build sessions finish the demo path; every claim true or cut |
| 09:00 | Fresh-clone check of the README commands |
| 10:00 | Video recorded and uploaded |
| 10:30 | Public copy pushed; About shows MIT |
| 11:00 | Devpost draft complete; read through once |
| 11:30 | **Submit** (30 minutes of slack) |
