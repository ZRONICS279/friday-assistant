# Security

Friday controls real things in real homes: TVs, lights, plugs, and food
orders charged to a card. Please report security problems
privately, not in a public issue.

**Report:** use GitHub's "Report a vulnerability" button on the Security
tab of this repository. You'll get a reply within a few days.

## How Friday is meant to be deployed

- On your **home network only**. Don't forward port 8787 on your router or
  expose it to the internet. For access away from home, use a VPN such as
  Tailscale or WireGuard.
- Every API call needs the house **pairing code** (`API_TOKEN` in `.env`).
  Anyone with that code can control the house, so treat it like a password.
- API keys and tokens live in `.env`, which git ignores. Never commit it.
- Supporting services (the model server, Whisper, Kokoro, MQTT, the Matter
  controller) listen on `127.0.0.1` only.
