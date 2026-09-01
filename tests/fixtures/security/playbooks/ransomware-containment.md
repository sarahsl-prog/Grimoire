---
title: Ransomware Containment
playbook: ransomware
phase: contain
severity: critical
action_type: manual
trigger: Ransomware encryption activity detected on a host
mitre_technique_id: T1486
platforms: [windows, linux]
---

# Ransomware Containment Playbook

## Trigger

Ransomware encryption activity detected on a host, typically via EDR or mass file-renaming alerts.

## Preparation

- Confirm EDR coverage on the affected host.
- Verify network segmentation rules are in place.

## Actions

1. Isolate the host from the network (EDR quarantine or switch port shutdown).
2. Preserve encrypted-file samples for later analysis.
3. Block command-and-control domains at the proxy.

## Containment

Disable the compromised account and revoke its active sessions across all IdP providers.

## Recovery

Rebuild the host from a known-good image and restore data from the last clean backup.
