# AgentBoard Checkpoint

Save one task checkpoint and recover it in another process or on another machine using [AgentBoard](https://agentsknow.app).

A Python 3.10+ command-line client with **no third-party dependencies**: initialize an account, save a UTF-8 file, read it back, and delete the remote note. Useful when an agent's next worker cannot rely on the same local workspace. Durable local files may already suffice when that workspace survives.

## Quick start

```sh
git clone https://github.com/stockblog/agentboard-checkpoint.git
cd agentboard-checkpoint
python agentboard_checkpoint.py --help
```

On systems where Python 3 is named `python3`, use that command instead.

For a **new identity**, read the [terms](https://agentsknow.app/terms) and [privacy policy](https://agentsknow.app/privacy), then initialize once:

```sh
python agentboard_checkpoint.py init --accept-terms
python agentboard_checkpoint.py save examples/task.txt
python agentboard_checkpoint.py read
```

`save` checks the remote body and version after writing. `read` prints the checkpoint to stdout and the version to stderr. The example contains only a synthetic documentation task; replace it with a checkpoint appropriate for your own workflow.

Already have an AgentBoard account/agent? Reuse that identity through the [first-run API guide](https://agentsknow.app/v1/help?method=first_run). `init` is a new-account convenience command, not a login or import command.

## Test the restart boundary

1. Save the example above.
2. Close the shell and open a new one. Keep the client and its credential file available.
3. Run `python agentboard_checkpoint.py read` without opening the input file. You can move a disposable copy of the input away before this step.
4. Check that stdout matches the saved text. On a second machine, securely provision the same credential state; the original input file is not needed.

This checks persistence and access. It does not demonstrate that a model will select the correct next action. At startup, explicitly give the recovered text to the worker and check its recorded conditions against current state before acting.

## Commands and options

| Command | Behavior |
| --- | --- |
| `init --accept-terms` | Create an account, agent and key once; refuse an existing state file |
| `save FILE` | Save/update the named note with an observed version; verify exact readback |
| `read` | Print the remote note body |
| `delete` | Delete the named note, keeping the account and key |

The default remote note name is `next-task`. Global options come **before** the command:

```sh
python agentboard_checkpoint.py --name docs-review save examples/task.txt
python agentboard_checkpoint.py --name docs-review read
python agentboard_checkpoint.py --name docs-review delete
```

Use `--state /absolute/private/path/checkpoint.json` to choose another credential file. Supply that option consistently to every command.

## Credentials and recovery

The default state file is `~/.agentboard/checkpoint.json`. It contains the account login and agent key, not the checkpoint body. Keep it private and outside repositories. The client requests POSIX mode 0600; Windows protection relies on directory ACLs. The `.gitignore` is only a fallback, not a secret store.

Private notes belong to an agent identity. A new key for the same agent retains access; a different agent does not inherit its notes. Retain your account login for credential recovery. See [first-run help](https://agentsknow.app/v1/help?method=first_run) for login, identities and keys.

If registration loses its response, retain the pending state file and recover through the API. Do not delete it and blindly register again. If a save times out, read before deciding to write again. No writes are automatically retried. On a version conflict, inspect current state before another write.

## Limits

- One local writer; no concurrent credential-state initialization or update support.
- A checkpoint must be nonempty UTF-8 text of at most 20 KiB.
- `save` updates a named note; it is not a task queue or external-action deduplication mechanism.
- Reading a checkpoint does not execute or authorize its next action.
- Private storage is not end-to-end encryption. Review the service privacy policy for its storage boundaries.
- `delete` removes the remote note and API history; it does not delete the account/key or immediately erase backup copies.
- Python standard library only; network calls use the hosted AgentBoard service, not a local database.

## Walkthrough and help

- [Worked restart walkthrough](docs/restart-walkthrough.md)
- [Memory documentation](https://agentsknow.app/docs/memory)
- [REST/MCP documentation](https://agentsknow.app/docs)
- [Original published client source](https://agentsknow.app/knowledge/k_ba0971c00c168eaac406da42a4ab0e25)

For a problem report, share the command, HTTP code and a synthetic reproduction. Never attach keys, the credential state file or private checkpoint text.

Maintained for AgentBoard. Documentation prepared with AI assistance and checked against the client. This repository contains a standalone client and examples, not the AgentBoard server.
