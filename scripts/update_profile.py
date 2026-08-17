#!/usr/bin/env python3
"""
Profile skill analyzer for github.com/VikasGutte

Reads every repository the token can see (public + authorized private), derives
skills from three independent signals, and rewrites the marked sections of
README.md.

Signals
    1. GitHub language byte counts        -> what you actually write
    2. Dependency manifests + file layout -> frameworks, databases, infra
    3. Recency of the last push           -> old work decays, current work wins

Privacy
    Private repository names, descriptions, URLs and source never leave this
    process. Only aggregated skill names and scores are written to README.md or
    skills.json. Debug output that could name a private repo is gated behind
    PROFILE_DEBUG=1, which you should not enable in a public Actions run.

Usage
    PROFILE_GITHUB_TOKEN=ghp_xxx python3 scripts/update_profile.py
    PROFILE_DRY_RUN=1 ... python3 scripts/update_profile.py   # print, don't write

No third-party dependencies. Python 3.9+.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration - tune these
# ---------------------------------------------------------------------------

USERNAME = "VikasGutte"

# Half-life of a skill, in days. A repo last pushed this long ago counts half as
# much as one pushed today. 18 months keeps a year of work strongly represented
# while letting 2019 experiments fade out.
HALF_LIFE_DAYS = 548

# Repos older than this contribute nothing at all.
MAX_AGE_DAYS = 365 * 5

# Floor so a genuinely old but large project still leaves a trace.
MIN_WEIGHT = 0.02

INCLUDE_FORKS = False
INCLUDE_ARCHIVED = False

# How many entries to show in each rendered section.
TOP_LANGUAGES = 8
TOP_TECHNOLOGIES = 14
TOP_BARS = 10

# Skills scoring below this (0-100 scale) are dropped as noise.
MIN_SCORE = 6

# GitHub reports these as "languages"; they are config, markup or diagram noise,
# not skills. Mermaid in particular shows up from diagrams in documentation.
EXCLUDED_LANGUAGES = {
    "Makefile", "Batchfile", "CMake", "Roff", "M4", "Dockerfile",
    "Procfile", "EJS", "Gherkin", "Nix", "Starlark",
    "Mermaid", "TeX", "Rich Text Format", "Handlebars", "Pug", "Blade",
}

# Skills that are true but say nothing about you on a profile.
SUPPRESSED_SKILLS = {"Git"}

# Pairs that are the same evidence counted twice. The key is folded into the
# value, taking the higher score rather than summing.
ABSORB = {
    "Docker Compose": "Docker",
    "CI/CD": "GitHub Actions",
    "E2E Testing": "Testing",
    "Dependency Injection": "Dagger/Hilt",
    "Jetpack": "Android",
}

API = "https://api.github.com"
DEBUG = os.environ.get("PROFILE_DEBUG") == "1"
DRY_RUN = os.environ.get("PROFILE_DRY_RUN") == "1"

README_PATH = os.path.join(os.path.dirname(__file__), "..", "README.md")
SKILLS_PATH = os.path.join(os.path.dirname(__file__), "..", "skills.json")


# ---------------------------------------------------------------------------
# Detection rules
# ---------------------------------------------------------------------------

# Presence of a file/path anywhere in the repo tree implies these technologies.
# Matched case-insensitively against the full path.
PATH_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("androidmanifest.xml",      ("Android",)),
    ("build.gradle",             ("Gradle",)),
    ("settings.gradle",          ("Gradle",)),
    ("pom.xml",                  ("Maven",)),
    ("dockerfile",               ("Docker",)),
    ("docker-compose",           ("Docker", "Docker Compose")),
    (".github/workflows/",       ("GitHub Actions", "CI/CD")),
    ("jenkinsfile",              ("Jenkins", "CI/CD")),
    (".gitlab-ci.yml",           ("CI/CD",)),
    ("pubspec.yaml",             ("Flutter", "Dart")),
    ("go.mod",                   ("Go",)),
    ("cargo.toml",               ("Rust",)),
    ("podfile",                  ("iOS", "CocoaPods")),
    (".xcodeproj",               ("iOS",)),
    (".xcworkspace",             ("iOS",)),
    ("package.swift",            ("Swift",)),
    ("tsconfig.json",            ("TypeScript",)),
    ("next.config",              ("Next.js",)),
    ("nuxt.config",              ("Nuxt",)),
    ("vite.config",              ("Vite",)),
    ("webpack.config",           ("Webpack",)),
    ("tailwind.config",          ("Tailwind CSS",)),
    ("angular.json",             ("Angular",)),
    ("nest-cli.json",            ("NestJS",)),
    ("svelte.config",            ("Svelte",)),
    ("manage.py",                ("Django",)),
    ("prisma/schema.prisma",     ("Prisma",)),
    ("firebase.json",            ("Firebase",)),
    (".firebaserc",              ("Firebase",)),
    ("vercel.json",              ("Vercel",)),
    ("netlify.toml",             ("Netlify",)),
    ("serverless.yml",           ("Serverless",)),
    ("app.json",                 ("React Native",)),
    ("metro.config",             ("React Native",)),
    ("capacitor.config",         ("Capacitor",)),
    (".tf",                      ("Terraform",)),
    ("helm/",                    ("Kubernetes", "Helm")),
    ("chart.yaml",               ("Kubernetes", "Helm")),
    ("k8s/",                     ("Kubernetes",)),
    ("kubernetes/",              ("Kubernetes",)),
    ("supabase/",                ("Supabase",)),
    ("gemfile",                  ("Ruby",)),
    ("composer.json",            ("PHP",)),
    ("cmakelists.txt",           ("CMake",)),
    ("requirements.txt",         ("Python",)),
    ("pyproject.toml",           ("Python",)),
]

# Manifests worth downloading and reading. Only files at these paths (or ending
# with them) are fetched, and only the first MANIFEST_LIMIT per repo.
MANIFEST_FILES = (
    "package.json",
    "build.gradle",
    "build.gradle.kts",
    "pom.xml",
    "requirements.txt",
    "pyproject.toml",
    "docker-compose.yml",
    "docker-compose.yaml",
    "pubspec.yaml",
    "go.mod",
    "cargo.toml",
    "composer.json",
    "gemfile",
)
MANIFEST_LIMIT = 6
MAX_BLOB_BYTES = 256 * 1024

# Regexes matched against downloaded manifest contents (lowercased).
CONTENT_RULES: list[tuple[str, tuple[str, ...]]] = [
    # --- JS / TS ecosystem ---
    (r'"react"\s*:',                     ("React",)),
    (r'"react-native"\s*:',              ("React Native",)),
    (r'"next"\s*:',                      ("Next.js",)),
    (r'"vue"\s*:',                       ("Vue.js",)),
    (r'"@angular/core"\s*:',             ("Angular",)),
    (r'"svelte"\s*:',                    ("Svelte",)),
    (r'"expo"\s*:',                      ("Expo", "React Native")),
    (r'"electron"\s*:',                  ("Electron",)),
    (r'"typescript"\s*:',                ("TypeScript",)),
    (r'"express"\s*:',                   ("Express", "Node.js", "REST APIs")),
    (r'"@nestjs/core"\s*:',              ("NestJS", "Node.js")),
    (r'"fastify"\s*:',                   ("Node.js", "REST APIs")),
    (r'"socket\.io"\s*:',                ("WebSockets",)),
    (r'"graphql"\s*:',                   ("GraphQL",)),
    (r'"@apollo/',                       ("GraphQL",)),
    (r'"prisma"\s*:|"@prisma/client"',   ("Prisma",)),
    (r'"mongoose"\s*:',                  ("MongoDB",)),
    (r'"mongodb"\s*:',                   ("MongoDB",)),
    (r'"pg"\s*:|"postgres"\s*:',         ("PostgreSQL",)),
    (r'"mysql2?"\s*:',                   ("MySQL",)),
    (r'"redis"\s*:|"ioredis"\s*:',       ("Redis",)),
    (r'"firebase"\s*:|"firebase-admin"', ("Firebase",)),
    (r'"@supabase/',                     ("Supabase",)),
    (r'"aws-sdk"|"@aws-sdk/',            ("AWS",)),
    (r'"tailwindcss"\s*:',               ("Tailwind CSS",)),
    (r'"vite"\s*:',                      ("Vite",)),
    (r'"jest"\s*:|"vitest"\s*:',         ("Testing",)),
    (r'"cypress"\s*:|"playwright"\s*:',  ("Testing", "E2E Testing")),
    (r'"redux"|"@reduxjs/toolkit"',      ("Redux",)),
    (r'"zustand"\s*:',                   ("React",)),
    (r'"axios"\s*:',                     ("REST APIs",)),
    (r'"stripe"\s*:',                    ("Stripe",)),
    (r'"three"\s*:',                     ("Three.js",)),

    # --- Android / Kotlin / Java ---
    (r'androidx\.',                      ("Android", "Jetpack")),
    (r'androidx\.compose|compose\.ui',   ("Jetpack Compose",)),
    (r'kotlinx-coroutines|kotlinx\.coroutines', ("Coroutines", "Kotlin")),
    (r'org\.jetbrains\.kotlin',          ("Kotlin",)),
    (r'com\.squareup\.retrofit',         ("Retrofit", "REST APIs")),
    (r'com\.squareup\.okhttp',           ("OkHttp",)),
    (r'androidx\.room|room-runtime',     ("Room", "SQLite")),
    (r'dagger|hilt',                     ("Dagger/Hilt", "Dependency Injection")),
    (r'com\.google\.firebase',           ("Firebase",)),
    (r'com\.github\.bumptech\.glide|io\.coil-kt', ("Android",)),
    (r'androidx\.navigation',            ("Jetpack",)),
    (r'androidx\.work',                  ("Jetpack",)),
    (r'org\.springframework\.boot',      ("Spring Boot", "Java", "REST APIs")),
    (r'org\.springframework',            ("Spring",)),
    (r'org\.hibernate',                  ("Hibernate",)),
    (r'junit|mockito|espresso',          ("Testing",)),
    (r'postgresql',                      ("PostgreSQL",)),
    (r'mysql-connector',                 ("MySQL",)),

    # --- Python ---
    (r'^\s*django|"django"',             ("Django", "Python")),
    (r'^\s*flask|"flask"',               ("Flask", "Python")),
    (r'^\s*fastapi|"fastapi"',           ("FastAPI", "Python", "REST APIs")),
    (r'sqlalchemy',                      ("SQLAlchemy",)),
    (r'psycopg2?',                       ("PostgreSQL",)),
    (r'pandas|numpy',                    ("Data Analysis",)),
    (r'tensorflow|torch|scikit-learn',   ("Machine Learning",)),
    (r'celery',                          ("Celery",)),
    (r'pytest',                          ("Testing",)),

    # --- docker-compose services ---
    (r'image:\s*[\'"]?postgres',         ("PostgreSQL",)),
    (r'image:\s*[\'"]?mysql',            ("MySQL",)),
    (r'image:\s*[\'"]?mongo',            ("MongoDB",)),
    (r'image:\s*[\'"]?redis',            ("Redis",)),
    (r'image:\s*[\'"]?nginx',            ("Nginx",)),
    (r'image:\s*[\'"]?rabbitmq',         ("RabbitMQ",)),
    (r'image:\s*[\'"]?elasticsearch',    ("Elasticsearch",)),
    (r'image:\s*[\'"]?kafka',            ("Kafka",)),

    # --- Flutter ---
    (r'flutter:\s*\n\s*sdk:',            ("Flutter", "Dart")),
    (r'cloud_firestore|firebase_core',   ("Firebase",)),
]

# Skill -> section in the "What I work with" block.
CATEGORIES: list[tuple[str, tuple[str, ...]]] = [
    ("Mobile", (
        "Android", "Kotlin", "Jetpack Compose", "Jetpack", "Coroutines",
        "Retrofit", "Room", "Dagger/Hilt", "OkHttp", "React Native", "Expo",
        "Flutter", "Dart", "iOS", "Swift", "CocoaPods", "Capacitor",
    )),
    ("Frontend", (
        "React", "Next.js", "Vue.js", "Angular", "Svelte", "Nuxt",
        "TypeScript", "JavaScript", "Redux", "Tailwind CSS", "HTML", "CSS",
        "SCSS", "Vite", "Webpack", "Three.js", "Electron",
    )),
    ("Backend", (
        "Node.js", "Express", "NestJS", "Spring Boot", "Spring", "Java",
        "Python", "Django", "Flask", "FastAPI", "Go", "PHP", "Ruby", "Rust",
        "REST APIs", "GraphQL", "WebSockets", "Celery", "Hibernate",
    )),
    ("Data", (
        "PostgreSQL", "MySQL", "MongoDB", "SQLite", "Redis", "Firebase",
        "Supabase", "Prisma", "SQLAlchemy", "Elasticsearch", "Kafka",
        "RabbitMQ", "Data Analysis", "Machine Learning",
    )),
    ("DevOps & Tools", (
        "Docker", "Docker Compose", "Kubernetes", "Helm", "Terraform",
        "GitHub Actions", "CI/CD", "Jenkins", "AWS", "Vercel", "Netlify",
        "Serverless", "Nginx", "Gradle", "Maven", "Git", "Shell",
        "Testing", "E2E Testing", "Dependency Injection", "Stripe",
    )),
]

# shields.io badge definitions: name -> (hex colour, simple-icons slug, logo colour)
BADGES: dict[str, tuple[str, str, str]] = {
    # languages
    "Java":         ("ED8B00", "openjdk", "white"),
    "Kotlin":       ("7F52FF", "kotlin", "white"),
    "JavaScript":   ("F7DF1E", "javascript", "black"),
    "TypeScript":   ("3178C6", "typescript", "white"),
    "Python":       ("3776AB", "python", "white"),
    "Dart":         ("0175C2", "dart", "white"),
    "Swift":        ("F05138", "swift", "white"),
    "Go":           ("00ADD8", "go", "white"),
    "Rust":         ("000000", "rust", "white"),
    "PHP":          ("777BB4", "php", "white"),
    "Ruby":         ("CC342D", "ruby", "white"),
    "C++":          ("00599C", "cplusplus", "white"),
    "C":            ("A8B9CC", "c", "black"),
    "C#":           ("512BD4", "csharp", "white"),
    "HTML":         ("E34F26", "html5", "white"),
    "CSS":          ("1572B6", "css3", "white"),
    "SCSS":         ("CC6699", "sass", "white"),
    "Shell":        ("4EAA25", "gnubash", "white"),
    "SQL":          ("4479A1", "mysql", "white"),
    # frameworks & platforms
    "Android":          ("3DDC84", "android", "white"),
    "Jetpack Compose":  ("4285F4", "jetpackcompose", "white"),
    "Jetpack":          ("3DDC84", "android", "white"),
    "Coroutines":       ("7F52FF", "kotlin", "white"),
    "React":            ("61DAFB", "react", "black"),
    "React Native":     ("61DAFB", "react", "black"),
    "Expo":             ("000020", "expo", "white"),
    "Next.js":          ("000000", "nextdotjs", "white"),
    "Vue.js":           ("4FC08D", "vuedotjs", "white"),
    "Angular":          ("DD0031", "angular", "white"),
    "Svelte":           ("FF3E00", "svelte", "white"),
    "Nuxt":             ("00DC82", "nuxtdotjs", "white"),
    "Flutter":          ("02569B", "flutter", "white"),
    "iOS":              ("000000", "apple", "white"),
    "Electron":         ("47848F", "electron", "white"),
    "Node.js":          ("339933", "nodedotjs", "white"),
    "Express":          ("000000", "express", "white"),
    "NestJS":           ("E0234E", "nestjs", "white"),
    "Spring Boot":      ("6DB33F", "springboot", "white"),
    "Spring":           ("6DB33F", "spring", "white"),
    "Django":           ("092E20", "django", "white"),
    "Flask":            ("000000", "flask", "white"),
    "FastAPI":          ("009688", "fastapi", "white"),
    "Hibernate":        ("59666C", "hibernate", "white"),
    "Retrofit":         ("48B983", "square", "white"),
    "OkHttp":           ("48B983", "square", "white"),
    "Redux":            ("764ABC", "redux", "white"),
    "Tailwind CSS":     ("06B6D4", "tailwindcss", "white"),
    "Vite":             ("646CFF", "vite", "white"),
    "Webpack":          ("8DD6F9", "webpack", "black"),
    "Three.js":         ("000000", "threedotjs", "white"),
    "GraphQL":          ("E10098", "graphql", "white"),
    "REST APIs":        ("005571", "fastapi", "white"),
    "WebSockets":       ("010101", "socketdotio", "white"),
    "Capacitor":        ("119EFF", "capacitor", "white"),
    "CocoaPods":        ("EE3322", "cocoapods", "white"),
    "Celery":           ("37814A", "celery", "white"),
    # data
    "PostgreSQL":       ("4169E1", "postgresql", "white"),
    "MySQL":            ("4479A1", "mysql", "white"),
    "MongoDB":          ("47A248", "mongodb", "white"),
    "SQLite":           ("003B57", "sqlite", "white"),
    "Room":             ("3DDC84", "android", "white"),
    "Redis":            ("DC382D", "redis", "white"),
    "Firebase":         ("FFCA28", "firebase", "black"),
    "Supabase":         ("3FCF8E", "supabase", "white"),
    "Prisma":           ("2D3748", "prisma", "white"),
    "SQLAlchemy":       ("D71F00", "sqlalchemy", "white"),
    "Elasticsearch":    ("005571", "elasticsearch", "white"),
    "Kafka":            ("231F20", "apachekafka", "white"),
    "RabbitMQ":         ("FF6600", "rabbitmq", "white"),
    "Data Analysis":    ("150458", "pandas", "white"),
    "Machine Learning": ("EE4C2C", "pytorch", "white"),
    # devops & tools
    "Docker":           ("2496ED", "docker", "white"),
    "Docker Compose":   ("2496ED", "docker", "white"),
    "Kubernetes":       ("326CE5", "kubernetes", "white"),
    "Helm":             ("0F1689", "helm", "white"),
    "Terraform":        ("7B42BC", "terraform", "white"),
    "GitHub Actions":   ("2088FF", "githubactions", "white"),
    "CI/CD":            ("2088FF", "githubactions", "white"),
    "Jenkins":          ("D24939", "jenkins", "white"),
    "AWS":              ("232F3E", "amazonwebservices", "white"),
    "Vercel":           ("000000", "vercel", "white"),
    "Netlify":          ("00C7B7", "netlify", "white"),
    "Serverless":       ("FD5750", "serverless", "white"),
    "Nginx":            ("009639", "nginx", "white"),
    "Gradle":           ("02303A", "gradle", "white"),
    "Maven":            ("C71A36", "apachemaven", "white"),
    "Git":              ("F05032", "git", "white"),
    "Testing":          ("C21325", "jest", "white"),
    "E2E Testing":      ("2EAD33", "cypress", "white"),
    "Dagger/Hilt":      ("2C4AA8", "dagger", "white"),
    "Dependency Injection": ("2C4AA8", "dagger", "white"),
    "Stripe":           ("635BFF", "stripe", "white"),
}


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------

def token() -> str:
    tok = os.environ.get("PROFILE_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not tok:
        sys.exit(
            "No token found. Set PROFILE_GITHUB_TOKEN (a fine-grained PAT with "
            "read access to the repositories you want analyzed)."
        )
    return tok


def log(message: str) -> None:
    """Safe log line. Always shown."""
    print(message, flush=True)


def debug(message: str) -> None:
    """May reference private repositories. Only shown when PROFILE_DEBUG=1."""
    if DEBUG:
        print(f"  [debug] {message}", flush=True)


def api_get(path: str, params: dict | None = None, raw_404_ok: bool = True):
    url = path if path.startswith("http") else API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    request = urllib.request.Request(url)
    request.add_header("Authorization", f"Bearer {token()}")
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    request.add_header("User-Agent", f"{USERNAME}-profile-analyzer")

    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code in (403, 429):
                # Secondary rate limit or abuse detection: back off and retry.
                wait = int(error.headers.get("Retry-After") or (5 * 2 ** attempt))
                log(f"  rate limited, waiting {wait}s")
                time.sleep(wait)
                continue
            if error.code == 404 and raw_404_ok:
                return None
            if error.code == 409:  # empty repository
                return None
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    return None


def api_paginated(path: str, params: dict) -> list:
    results: list = []
    page = 1
    while True:
        batch = api_get(path, {**params, "per_page": 100, "page": page})
        if not batch:
            break
        results.extend(batch)
        if len(batch) < 100:
            break
        page += 1
        if page > 20:  # 2000 repos is plenty
            break
    return results


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def days_since(iso_timestamp: str) -> float:
    when = datetime.strptime(iso_timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    return (datetime.now(timezone.utc) - when).total_seconds() / 86400


def recency_weight(pushed_at: str | None) -> float:
    """Exponential decay on the last push date."""
    if not pushed_at:
        return MIN_WEIGHT
    age = days_since(pushed_at)
    if age > MAX_AGE_DAYS:
        return 0.0
    return max(MIN_WEIGHT, 0.5 ** (age / HALF_LIFE_DAYS))


def select_repos() -> tuple[list[dict], int]:
    """Returns (repos worth analysing, total repos the token can see)."""
    repos = api_paginated(
        "/user/repos",
        {
            "affiliation": "owner,collaborator,organization_member",
            "sort": "pushed",
        },
    )

    selected = []
    for repo in repos:
        if repo.get("fork") and not INCLUDE_FORKS:
            continue
        if repo.get("archived") and not INCLUDE_ARCHIVED:
            continue
        if repo.get("size", 0) == 0:
            continue
        if recency_weight(repo.get("pushed_at")) <= 0:
            continue
        selected.append(repo)
    return selected, len(repos)


def repo_tree(repo: dict) -> list[str]:
    """Full file list for the default branch. Cheap: one API call."""
    branch = repo.get("default_branch") or "main"
    data = api_get(
        f"/repos/{repo['full_name']}/git/trees/{urllib.parse.quote(branch)}",
        {"recursive": "1"},
    )
    if not data or "tree" not in data:
        return []
    return [
        (entry["path"], entry.get("sha"), entry.get("size") or 0)
        for entry in data["tree"]
        if entry.get("type") == "blob"
    ]


def read_blob(repo: dict, sha: str) -> str:
    data = api_get(f"/repos/{repo['full_name']}/git/blobs/{sha}")
    if not data or data.get("encoding") != "base64":
        return ""
    try:
        return base64.b64decode(data["content"]).decode("utf-8", errors="ignore")
    except Exception:
        return ""


def detect_from_tree(entries) -> tuple[set[str], list[tuple[str, str]]]:
    """Return (technologies implied by paths, manifests worth downloading)."""
    found: set[str] = set()
    manifests: list[tuple[str, str]] = []

    for path, sha, size in entries:
        lowered = path.lower()
        for needle, techs in PATH_RULES:
            if needle in lowered:
                found.update(techs)

        basename = lowered.rsplit("/", 1)[-1]
        if basename in MANIFEST_FILES and 0 < size <= MAX_BLOB_BYTES:
            # Prefer shallow manifests: app/build.gradle over deep vendor copies.
            manifests.append((lowered.count("/"), path, sha))

    manifests.sort(key=lambda item: item[0])
    return found, [(path, sha) for _, path, sha in manifests[:MANIFEST_LIMIT]]


def detect_from_content(text: str) -> set[str]:
    found: set[str] = set()
    lowered = text.lower()
    for pattern, techs in CONTENT_RULES:
        if re.search(pattern, lowered, re.MULTILINE):
            found.update(techs)
    return found


def analyze() -> tuple[dict[str, float], dict[str, float], dict]:
    repos, total_visible = select_repos()
    log(f"Analyzing {len(repos)} of {total_visible} visible repositories...")

    language_score: dict[str, float] = defaultdict(float)
    technology_score: dict[str, float] = defaultdict(float)

    stats = {
        "repositories_analyzed": len(repos),
        "repositories_visible": total_visible,
        "public": sum(1 for r in repos if not r.get("private")),
        "private": sum(1 for r in repos if r.get("private")),
        "active_last_90_days": 0,
    }

    for index, repo in enumerate(repos, start=1):
        weight = recency_weight(repo.get("pushed_at"))
        if repo.get("pushed_at") and days_since(repo["pushed_at"]) <= 90:
            stats["active_last_90_days"] += 1

        # Never print the repo name unless debugging - Actions logs on a public
        # repository are public.
        label = repo["full_name"] if DEBUG else f"repo {index}/{len(repos)}"
        debug(f"{label} weight={weight:.2f} private={bool(repo.get('private'))}")

        # --- signal 1: languages -------------------------------------------
        languages = api_get(f"/repos/{repo['full_name']}/languages") or {}
        total_bytes = sum(languages.values())
        if total_bytes:
            # log-scale the repo size so one huge repo cannot dominate, and use
            # each language's share within the repo rather than raw bytes.
            size_factor = math.log10(total_bytes + 10)
            for language, byte_count in languages.items():
                if language in EXCLUDED_LANGUAGES:
                    continue
                share = byte_count / total_bytes
                if share < 0.05:  # ignore stray files
                    continue
                language_score[language] += share * size_factor * weight

        # --- signals 2 and 3: files and manifests --------------------------
        entries = repo_tree(repo)
        path_techs, manifests = detect_from_tree(entries)
        content_techs: set[str] = set()
        for path, sha in manifests:
            content_techs |= detect_from_content(read_blob(repo, sha))

        # Manifest evidence is stronger than a filename match.
        for tech in path_techs:
            technology_score[tech] += 1.0 * weight
        for tech in content_techs:
            technology_score[tech] += 1.6 * weight

    languages = normalize(collapse(language_score))
    technologies = normalize(collapse(technology_score))

    # A language detected again through a manifest ("typescript" in
    # package.json) is the same skill, not a second one. Keep it in the
    # Languages row only.
    technologies = {
        name: score for name, score in technologies.items() if name not in languages
    }

    return languages, technologies, stats


def collapse(scores: dict[str, float]) -> dict[str, float]:
    """Drop filler skills and fold duplicate pairs into one entry."""
    merged: dict[str, float] = {}
    for name, value in scores.items():
        if name in SUPPRESSED_SKILLS:
            continue
        target = ABSORB.get(name, name)
        # Same evidence seen twice - take the stronger reading, don't add them.
        merged[target] = max(merged.get(target, 0.0), value)
    return merged


def normalize(scores: dict[str, float]) -> dict[str, float]:
    """Rescale to 0-100 against the strongest skill, then drop the noise."""
    if not scores:
        return {}
    top = max(scores.values())
    if top <= 0:
        return {}
    scaled = {
        name: round(100 * value / top, 1)
        for name, value in scores.items()
        if 100 * value / top >= MIN_SCORE
    }
    return dict(sorted(scaled.items(), key=lambda item: -item[1]))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def badge(name: str) -> str:
    color, logo, logo_color = BADGES.get(name, ("555555", "", "white"))
    label = urllib.parse.quote(name.replace("-", "--").replace("_", "__"))
    url = f"https://img.shields.io/badge/{label}-{color}?style=for-the-badge"
    if logo:
        url += f"&logo={logo}&logoColor={logo_color}"
    return f'<img alt="{name}" src="{url}" />'


def render_badges(names) -> str:
    return "\n".join(badge(name) for name in names)


def render_bars(scores: dict[str, float], limit: int) -> str:
    items = list(scores.items())[:limit]
    if not items:
        return "No data yet."
    width = max(len(name) for name, _ in items)
    lines = []
    for name, score in items:
        filled = max(1, round(score / 5))  # 20 cells at 100
        bar = "█" * filled + "░" * (20 - filled)
        lines.append(f"{name.ljust(width)}  {bar}  {score:>5.1f}")
    return "\n".join(lines)


def render_stack(combined: dict[str, float]) -> str:
    lines = []
    for title, members in CATEGORIES:
        present = [name for name in combined if name in members]
        present.sort(key=lambda name: -combined[name])
        if present:
            lines.append(f"{title.ljust(16)} → " + " · ".join(present[:8]))
    return "\n".join(lines) if lines else "No data yet."


def replace_block(text: str, marker: str, body: str) -> str:
    start, end = f"<!-- {marker}:START -->", f"<!-- {marker}:END -->"
    pattern = re.escape(start) + r".*?" + re.escape(end)
    if not re.search(pattern, text, re.DOTALL):
        log(f"  warning: marker {marker} not found in README, skipping")
        return text
    return re.sub(pattern, f"{start}\n\n{body}\n\n{end}", text, flags=re.DOTALL)


def update_readme(languages, technologies, stats) -> None:
    with open(README_PATH, "r", encoding="utf-8") as handle:
        readme = handle.read()

    top_languages = list(languages)[:TOP_LANGUAGES]
    top_technologies = list(technologies)[:TOP_TECHNOLOGIES]

    skills = (
        "**Languages**\n\n<p>\n"
        + render_badges(top_languages)
        + "\n</p>\n\n**Frameworks, tools & platforms**\n\n<p>\n"
        + render_badges(top_technologies)
        + "\n</p>"
    )
    readme = replace_block(readme, "SKILLS", skills)

    combined = {**languages, **technologies}
    combined = dict(sorted(combined.items(), key=lambda item: -item[1]))

    stack = (
        "```text\n"
        + render_bars(combined, TOP_BARS)
        + "\n```\n\n```text\n"
        + render_stack(combined)
        + "\n```"
    )
    readme = replace_block(readme, "STACK", stack)

    now = datetime.now(timezone.utc).strftime("%d %b %Y")
    activity = (
        f"| Active repositories | Pushed in last 90 days | Skills detected |\n"
        f"| :---: | :---: | :---: |\n"
        f"| {stats['repositories_analyzed']} of {stats['repositories_visible']} | "
        f"{stats['active_last_90_days']} | {len(combined)} |\n\n"
        f"<sub>Derived from language statistics, dependency manifests and push "
        f"recency across my public and private repositories. Private repository "
        f"names, descriptions and source are never published — only the "
        f"aggregated skills above. Last analysed **{now}**.</sub>"
    )
    readme = replace_block(readme, "ACTIVITY", activity)

    if DRY_RUN:
        log("\n--- dry run, README not written ---\n")
        log(readme)
        return

    with open(README_PATH, "w", encoding="utf-8") as handle:
        handle.write(readme)
    log("README.md updated.")


def write_skills_json(languages, technologies, stats) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stats": stats,
        "languages": languages,
        "technologies": technologies,
    }
    if DRY_RUN:
        log(json.dumps(payload, indent=2))
        return
    with open(SKILLS_PATH, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    log("skills.json updated.")


def main() -> None:
    languages, technologies, stats = analyze()

    log("\nLanguages:")
    log(render_bars(languages, TOP_LANGUAGES))
    log("\nTechnologies:")
    log(render_bars(technologies, TOP_TECHNOLOGIES))
    log("")

    if not languages and not technologies:
        sys.exit("No skills detected - check the token's repository access.")

    update_readme(languages, technologies, stats)
    write_skills_json(languages, technologies, stats)


if __name__ == "__main__":
    main()
