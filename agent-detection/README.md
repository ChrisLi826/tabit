# Agent detection manifests

Vendored from [herdr](https://github.com/herdrdev/herdr) agent-detection rules
(`src/detect/manifests/` / `website/agent-detection/`, also published at
https://herdr.dev/agent-detection/).

Used by tabit to classify AI tab status (`idle` / `working` / `blocked`)
without requiring the herdr binary.

- Offline: these files ship with tabit.
- Optional online refresh: once per day into `~/.config/tabit/agent-detection/`
  when `agent_manifest_check` is true in settings (default on).
