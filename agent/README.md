# Sarvam agent configuration

Two files, both pasted into the Sarvam Voice Agents console rather than deployed
with the service.

**`sarvam_instruction.txt`** goes into the agent's Instruction field verbatim.
It is plain text with no markdown, because the platform scans the raw string for
`call tool:name` and a stray backtick or trailing full stop becomes part of the
tool name it tries to resolve. Every tool reference in the file is followed by a
space or a line break for that reason — worth preserving if you edit it.

**`tools.json`** describes the seven API tools to register as live actions. It is
a reference for filling in the console form, not something the platform imports.
Tool names must match the instruction exactly, and the descriptions are
load-bearing: the model chooses when to call a tool from its name and
description, so they are written for the model rather than as documentation.

## Setup notes

The session is opened by a `start_session` tool with the `on_start` lifecycle,
which fires before the conversation begins and saves `session_id` into an agent
variable. Every other tool passes that variable. Making it an `on_start` hook
rather than a conversational tool means the model cannot forget to open a
session.

Each tool needs `@agent_message` as its response template. The service already
returns speakable wording, and for regulated content it is the only approved
wording — the model should deliver it rather than paraphrase.

Forward the platform's interaction id as `x-trace-id` on every tool call. The
service adopts it, so one identifier then spans Sarvam's call analytics and the
service's structured logs.
