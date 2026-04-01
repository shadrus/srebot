import re
from typing import Any

# The text from the user's log (cleaned up from timestamps if they are log prefixes)
TEXT = """
[RESOLVED] KubePodNotReady

*Alert:* Pod has been in a non-ready state for more than 15 minutes. - `warning`

*Description:* Pod canton/validator-app-7d77c99ccb-w7wlc has been in a non-ready state for longer than 15 minutes on cluster management-cluster.

*Graph:* *Runbook:* <|:spiral_note_pad:>

*Details:*
 • *alertname:* `KubePodNotReady`
 • *alertgroup:* `kubernetes-apps`
 • *cluster:* `management-cluster`
 • *namespace:* `canton`
 • *pod:* `validator-app-7d77c99ccb-w7wlc`
 • *severity:* `warning`
"""

# Regexes from the Markdown strategy in DB
RESOLVED_PAT = r"(alerts?\s+resolved|\[RESOLVED:.*\]|RESOLVED|✅|\[RESOLVED\])"
FIRING_PAT = r"(alerts?\s+firing|\[FIRING:.*\]|FIRING|🔥|\*Alert:\*)"
LABELS_HEADER_PAT = r"^\*?(Details|Labels):\*?\s*$"
KV_PAT = r"^\s*\*?\s*[•\-]?\s*\*?\s*(.+?)\s*\*?\s*[:=]\*?\s*(.+)$"

def debug():
    print("--- Debugging Markdown Strategy ---")
    
    # 1. Status Check
    resolved_re = re.compile(RESOLVED_PAT, re.IGNORECASE)
    firing_re = re.compile(FIRING_PAT, re.IGNORECASE)
    
    if resolved_re.search(TEXT):
        print("[OK] Status: RESOLVED")
    elif firing_re.search(TEXT):
        print("[OK] Status: FIRING")
    else:
        print("[FAIL] Status not found")
        return

    # 2. Split Blocks
    labels_header_re = re.compile(LABELS_HEADER_PAT, re.IGNORECASE | re.MULTILINE)
    split_pattern = f"(?={labels_header_re.pattern})"
    parts = re.split(split_pattern, TEXT, flags=re.MULTILINE | re.IGNORECASE)
    
    print(f"DEBUG: Found {len(parts)} parts after split")
    for i, p in enumerate(parts):
        print(f"DEBUG: Part {i} matches header: {bool(labels_header_re.search(p))}")
        if labels_header_re.search(p):
            print(f"DEBUG: Part {i} sample: {p[:50]!r}...")

    blocks = [p.strip() for p in parts if labels_header_re.search(p)]
    print(f"Found {len(blocks)} blocks")

    if not blocks:
        print("[FAIL] No blocks found with labels header pattern!")
        return

    # 3. KV Parsing
    kv_re = re.compile(KV_PAT, re.IGNORECASE)
    for block in blocks:
        print(f"--- Block KV Parsing ---")
        lines = block.splitlines()
        for line in lines:
            m = kv_re.match(line)
            if m:
                print(f"  [KV MATCH] '{line}' -> {m.groups()}")
            else:
                # Optionally check if the line is just the header
                if labels_header_re.match(line):
                    print(f"  [HEADER] '{line}'")
                else:
                    print(f"  [NO MATCH] '{line}'")

if __name__ == "__main__":
    debug()
