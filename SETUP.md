# Setting up the self-updating profile

The `VikasGutte/VikasGutte` repository is already public and already displays its
`README.md` on your profile page, so there is nothing to create — you only need
to push these files and add one secret.

```
VikasGutte/
├── README.md                          the public profile page
├── skills.json                        generated: aggregated scores, no repo names
├── SETUP.md                           this file
├── scripts/
│   └── update_profile.py              the analyzer (standard library only)
└── .github/workflows/
    └── update-profile.yml             runs weekly + on demand
```

---

## 1. Create the read-only token

GitHub's API only returns your private repositories to an authenticated caller,
so the analyzer needs a token of its own. The Actions-provided `GITHUB_TOKEN`
can only see this one repository, which is why a PAT is required.

**Settings → Developer settings → Personal access tokens → Fine-grained tokens →
Generate new token**

| Field | Value |
| --- | --- |
| Token name | `profile-analyzer` |
| Expiration | 90 days (or custom — you will need to rotate it) |
| Resource owner | `VikasGutte` |
| Repository access | **Only select repositories** → tick every repo you want counted, public and private |
| Permissions | **Repository permissions → Contents: Read-only** and **Metadata: Read-only** |

Nothing else. No write permissions, no account permissions.

> `Contents: Read-only` is what lets the analyzer read `package.json`,
> `build.gradle` and friends. Without it you still get language statistics, but
> framework detection goes quiet.

Copy the token — GitHub shows it once.

## 2. Store it as a repository secret

**VikasGutte repo → Settings → Secrets and variables → Actions → New repository
secret**

- Name: `PROFILE_GITHUB_TOKEN`
- Secret: the token from step 1

The token must never appear in `README.md`, in the Python file, in the workflow
YAML, or in a commit. Secrets are the only correct place for it.

## 3. Push the files

```bash
cd VikasGutte
git add README.md SETUP.md .gitignore scripts .github
git commit -m "feat: self-updating profile with repository-derived skills"
git push origin main
```

## 4. Run it once

**Actions → Update profile skills → Run workflow**

The run checks out the repo, analyses your repositories, rewrites the three
marked sections of `README.md`, writes `skills.json`, and commits only if
something changed. After that it runs every Monday at 03:00 UTC on its own.

## 5. Pin 3–5 repositories

The analyzer can't fix what your pinned repos say about you. On your profile
page, **Customize your pins** and choose the work you actually want judged on —
current projects first, old HTML/CSS experiments last.

---

## Running it locally

```bash
export PROFILE_GITHUB_TOKEN=github_pat_xxx
PROFILE_DRY_RUN=1 python3 scripts/update_profile.py   # print, change nothing
python3 scripts/update_profile.py                     # actually rewrite README
```

`PROFILE_DEBUG=1` adds per-repository scoring output. **Only use it locally.**
Actions logs on a public repository are public, and debug output names private
repositories.

---

## How the scoring works

Three signals, combined per repository and then weighted by how recently you
pushed to it:

| Signal | Source | Weight |
| --- | --- | --- |
| Languages | `/repos/{repo}/languages` byte counts | share within the repo × log(repo size) |
| File layout | recursive git tree — `AndroidManifest.xml`, `Dockerfile`, `.github/workflows/`, … | 1.0 per match |
| Dependencies | `package.json`, `build.gradle`, `pom.xml`, `requirements.txt`, `docker-compose.yml`, `pubspec.yaml`, … | 1.6 per match |

Recency decay is exponential with a **548-day half-life**: a repo you pushed to
today counts twice as much as one from 18 months ago, and anything past 5 years
counts for nothing. Language scores use each language's *share* of its repo
rather than raw bytes, so one enormous project can't flatten everything else.
Scores are then rescaled 0–100 against your top skill, and anything under 6 is
dropped.

Tuning constants live at the top of `scripts/update_profile.py`:
`HALF_LIFE_DAYS`, `MAX_AGE_DAYS`, `MIN_SCORE`, `TOP_LANGUAGES`,
`TOP_TECHNOLOGIES`, `INCLUDE_FORKS`, `EXCLUDED_LANGUAGES`.

To teach it a new technology, add a row to `PATH_RULES` (file present ⇒ skill)
or `CONTENT_RULES` (regex against a manifest ⇒ skill), plus an entry in
`BADGES` and the right `CATEGORIES` tuple. Unknown skills still render — as a
plain grey badge — so a missing badge entry is cosmetic, not a failure.

## What is and isn't published

Published: skill names, 0–100 scores, repository counts, a timestamp.

Never published: repository names, descriptions, URLs, owners, commit messages,
branch names, file paths, or any source code. The analyzer holds those in memory
only; `skills.json` and `README.md` contain aggregates exclusively. Verify for
yourself with `PROFILE_DRY_RUN=1`.

## If the run fails

| Symptom | Cause |
| --- | --- |
| `No token found` | Secret missing or misnamed — it must be exactly `PROFILE_GITHUB_TOKEN` |
| `No skills detected` | Token has no repository access selected, or it expired |
| Only public repos counted | Private repos weren't ticked under "Only select repositories" |
| Frameworks missing, languages fine | Token lacks `Contents: Read-only` |
| `marker … not found in README` | A `<!-- SKILLS:START -->` style comment was deleted — restore the pair |
| Nothing commits | Scores were identical to last week; that's the intended no-op |
