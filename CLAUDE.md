# Karaokemp library cleanup project

This repo is the pipeline that cleaned up and organized the Karaokemp (Israeli Burning Man karaoke camp) media library. It is now a **published reference archive**. Cutover has happened, the runtime DB is authoritative, and new material goes through runtime ingest, not this pipeline. Start with @README.md.

- Runtime DB contract: `docs/runtime-db-spec.md`
- Build log, including deviations, retunes and test-pinned traps: `docs/history/HISTORY.md` (large; search it, don't load it whole)
- Original spec: `docs/history/original-spec-v2.md`

This is a public repo. Never commit personal data, third-party emails, Drive folder or file ids, or API keys. Configuration that identifies anyone comes from env vars (see README).

When monitoring long-running processes, try to conserve tokens - don't poll every fixed amount of time. Test 30sec after start, 5 minutes, and then at 25%, 50%, 75% if the task is supposed to take more than 30min.
