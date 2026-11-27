# Agent notes

Second Brain is local-first. Do not send the vault to a cloud client.

What you may store is the capture policy in `docs/CAPTURE_POLICY.md` and `docs/capture_policy.json`. One atom is one durable, checkable fact: a decision, config value, port, path, host, absolute date, person and role, preference, procedure, todo, or measurement. Do not store mood, filler, headings, markup, fragments, secrets, or relative dates such as "tomorrow". Evidence stays a verbatim span of the source note. Uncertain facts are marked tentative and are not auto-approved.

The local MCP server lists that policy at `brain://policy/capture`. `remember` and `add_to_inbox` reject a bad submission with the reasons, so fix the text and resubmit. There are still 35 MCP tools. Do not register the server with cloud-connected clients.
