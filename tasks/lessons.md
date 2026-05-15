# Lessons

Patterns and rules learned from user corrections. Reviewed at session start.

Format for each entry:
- **Trigger** — what the user pushed back on
- **Rule** — what to do differently next time
- **Why** — the reason (so edge cases can be judged later)

---

## Explicit voice/chat commands are themselves the confirmation
- **Trigger**: User complained that Tier 3 file ops felt slow — every action triggered a Yes/No prompt even though they had just *explicitly asked for it* by voice. Saying "create folder X" and then having to say "yes" to "create folder X?" felt absurd and added two Claude round-trips of latency.
- **Rule**: For *single-user agentic* setups, per-action confirmation is only useful when the agent might be doing something the user didn't directly request (e.g. inferred steps, prompt-injection defence). When the user's exact intent IS the trigger, the confirmation is friction without safety. Default-off should still apply for shared / public deployments.
- **How to apply**: Surface a setting (here: `MAC_TIER3_AUTO_CONFIRM`) so the operator chooses. Keep Tier 4 password gate untouched — that's defending against impersonation, not user error.

## User explicitly trades safety for capability — respect that
- **Trigger**: After the staged allowlist landed, user said "Mache es so, das Jarvis mit meiner Erlaubnis alle Apps auf diesem PC öffnen und auch voll bedienen kann. Keine Einschränkungen mehr." They want the full control surface, gated only by the Tier-4 password.
- **Rule**: When the operator owns the machine, owns the consequences, and *explicitly* asks for a less-restrictive setting, build the less-restrictive setting cleanly — don't smuggle in extra confirmation prompts as a "safety net". State the new threat model in plain language so they can decide with full information, then proceed.
- **Why**: A "safety" feature that the user finds infuriating is a feature they'll disable in worse ways (env-var hacks, copy-paste workarounds, fork). Honest defaults > paternalistic defaults.
- **How to apply**: For single-user agentic systems: allowlists/blocklists are useful as defaults, but per-user override has to be a one-line config change, not a code edit. When implementing the override, also surface the trade-off in docs (e.g. "logging now records AppleScript content; prompt-injection surface widens"). [[mac-control-relaxation]]

## Audio feedback closes the loop the user expects
- **Trigger**: After confirming an action in the web UI (button click → /confirm), JARVIS ran silently. User wanted spoken success confirmation like every other path provides.
- **Rule**: Every action-completion path needs *some* feedback channel. Voice path → server-side TTS. Browser path → client-side SpeechSynthesis. Don't assume the user is watching the UI.
- **How to apply**: Whenever adding a new "fire and complete" endpoint, ask: which client surface is the user looking at, and which speaks the result? If none speaks it, the loop is incomplete.
