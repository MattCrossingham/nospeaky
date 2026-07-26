# NoSpeaky website workflow

## Golden rules

1. All work in:
   `~/workspace/PiStudios/Web Development/nospeaky/`
2. `git pull` before edits
3. Tag a backup before risky changes
4. Prefer branch + PR into `main`
5. Never force-push `main` unless Matt explicitly orders it after a known-good tag

## Restore

```bash
git checkout backup-YYYYMMDD-HHMMSS
# then open PR or carefully reset main only with explicit approval
```

## Pages

- Branch: `main`
- Folder: `/` (root)
- CNAME file: `nospeaky.ai`
