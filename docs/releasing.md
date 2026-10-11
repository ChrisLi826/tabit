# Releasing (cuisine codenames)

Each release gets a fresh **Taiwan-origin cuisine** English codename. There is
**no public menu of upcoming names** in the repo (that would spoil the
surprise). RD picks the name when cutting the release.

Public label:

```text
v1.7.12 · Oyster Omelette
```

That same string appears in:

- GitHub Release **title** (and preferably the notes heading)
- In-app **Update** dialog (`New version: …`)
- **Settings** version line (About)

## Naming rules

| Rule | Detail |
|---|---|
| Origin | Must be **Taiwan-origin cuisine** (not limited to street snacks) |
| Major / minor | 1st and 2nd version digits → more **internationally known** names |
| Patch | 3rd digit → more **obscure** names |
| Language | **English** display name in the public label; optional Chinese only in private notes if needed — never a browsable public catalog |
| Reuse | Do not reuse a name already in shipped history — except a **hotfix**, which is the same release with bugs taken out and keeps its name (`v1.8.3` → `v1.8.3.1`) |

## Files

| File | Role |
|---|---|
| `APP_VERSION` / `APP_CODENAME` in `tabit.py` | What the running build shows |
| `release-codenames.json` | **Shipped history only** (no future candidates) |
| `scripts/release-codename.py` | `list` / `title` / `record` / `check` helpers |

## Checklist when cutting a release

1. Pick a fresh English codename per the rules above (`python3 scripts/release-codename.py list` shows names already used).
2. Bump `APP_VERSION` and set `APP_CODENAME` in `tabit.py` to that English name.
3. Record history (no candidate pool):  
   `python3 scripts/release-codename.py record vX.Y.Z --name "English Name"`
4. Commit, tag `vX.Y.Z`, and create the GitHub release **with the codename in the title**:

```sh
TITLE=$(python3 scripts/release-codename.py title)
gh release create vX.Y.Z -t "$TITLE" -F .release/vX.Y.Z-notes.md
```

5. Confirm four places spell the **same** `vX.Y.Z · Name` string:
   - `APP_CODENAME` in `tabit.py`
   - shipped entry in `release-codenames.json`
   - Settings → Version (About)
   - Check for Updates / Update dialog (and GitHub release title)
