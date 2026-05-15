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

## Audio feedback closes the loop the user expects
- **Trigger**: After confirming an action in the web UI (button click → /confirm), JARVIS ran silently. User wanted spoken success confirmation like every other path provides.
- **Rule**: Every action-completion path needs *some* feedback channel. Voice path → server-side TTS. Browser path → client-side SpeechSynthesis. Don't assume the user is watching the UI.
- **How to apply**: Whenever adding a new "fire and complete" endpoint, ask: which client surface is the user looking at, and which speaks the result? If none speaks it, the loop is incomplete.
