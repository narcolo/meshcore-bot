# Scope Hint Command

The **Scope Hint Command** watches a configured channel (default: Public) and warns senders whose messages arrive as plain unscoped `FLOOD` to switch to scoped (`TC_FLOOD`) routing.

It is a transition-period nudge: the mesh is moving toward requiring regional scopes (e.g. `#pl-podlasie`), and unscoped messages will eventually be refused. The command has no keywords — it is invoked automatically from the message handler and is excluded from the generic command pipeline (`automatic_hook_only`), so it never appears to execute as a user command.

## Configuration

Add this section to your `config.ini`:

```ini
[Scope_Hint_Command]
enabled = false
channel = Public
cooldown_hours = 24
response_scope = #pl-podlasie
allow_unscoped_response = false
```

## Options

- **enabled**: Set `true` to activate (default: `false`).
- **channel**: Channel to watch, matched case-insensitively (default: `Public`). Deliberately singular — do **not** add a `channels` key to this section; that would register the channels with the generic per-command channel-override mechanism and open them to general command processing.
- **cooldown_hours**: Hours before the same companion can be warned again (default: `24`; values below 1 fall back to 24 with a logged warning).
- **response_scope**: Named scope used for the warning transmission itself. Canonical lowercase; `#` is prepended if missing. Explicit global markers (`*`, `0`, `None`) force a global response and require `allow_unscoped_response = true`. Empty falls back to `[Channels] outgoing_flood_scope_override`.
- **allow_unscoped_response**: Explicitly accept sending the warning as unscoped global FLOOD (e.g. so unscoped-only listeners are reachable during the transition). Without this, the command **disables itself** when no named scope is in effect — a warning about unscoped traffic must not itself be an unscoped packet.

## Public channel authorization

Enabling this command for the Public channel requires the same explicit override as `monitor_channels`:

```ini
[Bot]
i_understand_that_running_the_bot_on_the_public_channel_is_potentially_disruptive_to_other_users_enjoyment_of_the_mesh_and_i_would_like_to_do_it_anyway = true
```

Without it, the command logs an error and disables itself at startup, and `--validate-config` reports an error.

## Correlation trust (when the warning can fire)

The unscoped/scoped signal comes from the RF packet's route type (`FLOOD` vs `TC_FLOOD`), correlated with the channel message. **Only packet-level correlation is trusted**: an exact or ≥16-hex-char partial match of the message's own packet prefix. Sender-level (pubkey) matches and the most-recent-packet fallback are never trusted — one companion can send both a scoped and an unscoped packet within the correlation window, and an automatic public warning must not be based on guesswork.

Consequence, by design: some genuinely unscoped messages are silently skipped when packet-level correlation is unavailable. The command prefers missing a warning over warning the wrong person.

## Response priority and rate limits

- On messages that trigger normal bot processing, the user's requested command response is sent **first**; the hint runs afterwards and yields to the bot-wide rate limiter. A hint rejected by rate limits is not recorded and simply retries on a later unscoped message.
- The hint bypasses only the sender's **per-user** rate-limit admission and does not consume the sender's per-user budget. Global, TX, and per-channel limits apply and are recorded normally (real airtime).

## Cooldown guarantees and limits

One warning per `cooldown_hours` per companion, **best-effort** (user-approved contract, 2026-07-18):

- The cooldown records a *successful* send only; failed sends may retry later.
- State is kept in memory and persisted to the `bot_metadata` table (`scope_hint_notified:*` keys), so it survives restarts.
- Duplicate warnings remain possible in rare windows: a crash between the radio send and the database write; a database write/read failure combined with a restart; loss of the metadata store. Within one process lifetime the in-memory record always enforces the window.

Companion identity is the sender's **full validated public key** when available (`pk:` namespace); otherwise the normalized sender name (`name:` namespace). Name identity is spoofable — channel text is unverified — so only the pubkey form is a real per-companion guarantee. Messages with no parsed sender (`Channel User`) are never warned.

When the migration ends, the whole `scope_hint_notified:*` keyspace in `bot_metadata` can be deleted; expired entries are otherwise simply ignored.

## Payload budget

The warning must fit MeshCore's channel-message body budget (`160 − bot username − 2`, minus 10 bytes for a regional-scope send). The command enforces this at runtime:

1. The full hint (`Hi @[name], enable scope #pl-podlasie …`) is used when it fits.
2. Oversized sender names fall back to a nameless short form (`hint_short`) — a mention is never truncated mid-name.
3. If even the short form exceeds the budget (oversized `response_scope` or translation), nothing is sent and no cooldown is recorded; an error is logged.

## Interaction with [Channels] flood_scopes

`scope_hint` observes unscoped messages **even when a named-only `flood_scopes` allowlist is configured** — it runs just before the allowlist drop. No `*` entry is needed, and all other command/keyword processing remains blocked for dropped messages.

Typical `#pl-podlasie` deployment sets both:

```ini
[Channels]
flood_scopes = #pl-podlasie

[Scope_Hint_Command]
response_scope = #pl-podlasie
```

## Translations

Keys under `commands.scope_hint` in `translations/*.json`: `hint` (named form, `{name}` + `{scope}` placeholders), `hint_short` (nameless fallback), `description`. Polish wording uses "region" for the thing to enable and "wiadomości bez zakresu" for unscoped messages.

## Future work

A later phase may add a per-channel expected-scope list (detecting *wrong*-scope messages, not just unscoped ones). Phase 1 detects only the presence or absence of any scope.
