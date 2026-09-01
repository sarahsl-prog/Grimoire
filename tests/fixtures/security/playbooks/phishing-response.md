# Phishing Response Playbook

## Trigger

User reports a suspicious email with a malicious link or attachment.

## Actions

1. Collect email headers and the original message.
2. Search mailboxes for the same sender and subject.
3. Purge reported messages from all mailboxes.
4. Block the sender domain at the mail gateway.

## Containment

Reset credentials for any user who clicked the link; revoke active sessions.
