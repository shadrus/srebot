# Carry public status with tool execution

The backend attaches an optional model-authored `progress_text` to the existing `execute_tools`
event, and the agent displays it only when the validated batch actually starts. This keeps the text
tied to real work, avoids a second LLM request and a separate progress protocol, and preserves mixed
backend/agent version compatibility through a deterministic local fallback.
