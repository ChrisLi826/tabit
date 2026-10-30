# Releasing (cuisine codenames)

Each release gets a **Taiwan-origin food** English codename in two parts: a
savoury main dish for the major version, and a snack or drink for the release
inside it. There is
**no public menu of upcoming names** in the repo (that would spoil the
surprise). RD picks the name when cutting the release.

Public label:

```text
v2.0.2 · Danzai Noodles + Mango Shaved Ice
```

Up to v2.0.1 a codename was one dish (`v1.7.12 · Oyster Omelette`).

That same string appears in:

- GitHub Release **title** (and preferably the notes heading)
- In-app **Update** dialog (`New version: …`)
- **Settings** version line (About)

## Naming rules

| Rule | Detail |
|---|---|
| Origin | Must be **Taiwan-origin** food |
| Form | `Main Dish + Snack`, joined by ` + ` |
| Main dish | A well-known **savoury main dish**. It belongs to the major and minor version (1st and 2nd digit): every `v2.1.x` release keeps the same one, and a new `v2.2.0` or `v3.0.0` picks a new one |
| Snack or drink | A Taiwan **snack, dessert or drink**. Every release picks a new one |
| Choosing | Let the name hint at the release: tie it to what the release adds or changes (a feature that ties tabs together, a fix that clears things up), or to the season it ships in (shaved ice in summer, ginger tea in winter). The same goes for a new main dish |
| Language | **English** display name in the public label; optional Chinese only in private notes if needed — never a browsable public catalog |
| Reuse | Do not reuse a snack or drink, or a main dish of another major version, already in shipped history — except a **hotfix**, which is the same release with bugs taken out and keeps its whole name (`v1.8.3` → `v1.8.3.1`) |

## Files

| File | Role |
|---|---|
| `APP_VERSION` / `APP_CODENAME` in `tabit.py` | What the running build shows |
| `release-codenames.json` | **Shipped history only** (no future candidates) |
| `scripts/release-codename.py` | `list` / `title` / `record` / `check` helpers |

## Checklist when cutting a release

1. Keep the main dish while the 1st and 2nd digit stay; pick a new one when either changes. Pick a fresh snack or drink per the rules above (`python3 scripts/release-codename.py list` shows names already used).
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
