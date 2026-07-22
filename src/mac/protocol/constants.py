TASK_STATUSES = {
    "proposed",
    "accepted",
    "running",
    "completed",
    "failed",
    "rejected",
    "cancelled",
    "superseded",
}

AUDIT_ACTIONS = {
    "register",
    "heartbeat",
    "submit_task",
    "claim_task",
    "propose_handoff",
    "accept_handoff",
    "reject_handoff",
    "start_task",
    "task_update",
    "submit_quality_result",
    "checkpoint_task",
    "retry_task",
    "cancel_task",
    "complete_task",
    "fail_task",
}

RISK_LEVELS = {"low", "medium", "high"}

ERROR_CODES = {
    "TASK_TYPE_UNSUPPORTED",
    "CONTEXT_URI_INVALID",
    "CAPABILITY_INSUFFICIENT",
    "PAYLOAD_TOO_LARGE",
    "SCHEMA_VERSION_MISMATCH",
    "MAX_HOPS_EXCEEDED",
    "TTL_EXPIRED",
    "CAS_CONFLICT",
    "AGENT_OFFLINE",
    "TRANSFER_REJECTED",
    "QUALITY_GATE_FAILED",
    "TASK_CANCELLED",
}
