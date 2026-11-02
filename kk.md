# Domain, port and tokens

## Domain
- `aitutor.100xsuite.com` → `http://localhost:9950`, through a Cloudflare tunnel on the server PC.
- The app connects to `wss://aitutor.100xsuite.com/ws/tutor`.
- The README and `deploy/cloudflared-tutor.yml` say `tutor.100xsuite.com`, but that address is not live. The live one is `aitutor.100xsuite.com`.
- If the domain returns 502, the tutor is not running. Start it with `./run.sh`.

## Tokens
1. **Cloudflare tunnel token**: kept in `~/.cloudflared/aitutor-token` on the server PC (not in this repo).
2. **`GERMAN_API_KEY`**: the app's `x-api-key`, set in `.env` (not in this repo). Without it every login check fails.
3. **Learner's Bearer token**: each session sends the user's own login token (from `/verify-login-otp` or `/auth/google`) in the `start` message. It is never stored.
4. **`LOG_KEY`** (optional, not set): needed to open `/logs.html` through the domain as `/logs.html?key=<LOG_KEY>`. Without it the logs page only opens on the server PC.
