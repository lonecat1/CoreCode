"""会话持久化：append-only JSONL 事件日志。

对外只需要记住四个动作：

    save_session(cwd, session_id, messages, already_saved_count)   # 追加未落盘的消息
    load_session(cwd, session_id)                                  # 恢复消息（不含 system）
    load_transcript(cwd, session_id)                               # 恢复 UI 条目（含压缩前历史）
    fork_session(cwd, session_id)                                  # 分叉成新会话

边界事件（压缩 / 剪裁 / 折叠）由 `append_*` 系列单独写入，它们不携带可恢复的
消息，而是告诉恢复算法「这里发生过什么」。

与原 TS 实现的差异见 docs/divergences.md 的 D11–D15。
"""

from .store import (
    BOUNDARY_EVENT_TYPES,
    EVENT_ASSISTANT,
    EVENT_COMPACT_BOUNDARY,
    EVENT_CONTEXT_COLLAPSE,
    EVENT_PROGRESS,
    EVENT_RENAME,
    EVENT_SNIP_BOUNDARY,
    EVENT_SUMMARY,
    EVENT_SYSTEM,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    EVENT_USER,
    MAX_TITLE_LENGTH,
    ROLE_TO_TYPE,
    SESSION_SUFFIX,
    ProjectMeta,
    SessionMeta,
    append_compact_boundary,
    append_context_collapse_span,
    append_snip_boundary,
    cleanup_expired_sessions,
    clear_session,
    ensure_message_id,
    extract_title_from_events,
    fork_session,
    list_all_projects,
    list_sessions,
    load_context_collapse_state,
    load_session,
    load_session_with_report,
    load_transcript,
    parse_events,
    project_dir,
    project_dir_name,
    read_existing_event_uuids,
    read_last_event_uuid,
    reconstruct_snipped_events,
    reconstruct_snipped_events_with_report,
    rename_session,
    save_session,
    session_file_path,
    unwrap_message,
)

__all__ = [
    "BOUNDARY_EVENT_TYPES",
    "EVENT_ASSISTANT",
    "EVENT_COMPACT_BOUNDARY",
    "EVENT_CONTEXT_COLLAPSE",
    "EVENT_PROGRESS",
    "EVENT_RENAME",
    "EVENT_SNIP_BOUNDARY",
    "EVENT_SUMMARY",
    "EVENT_SYSTEM",
    "EVENT_TOOL_CALL",
    "EVENT_TOOL_RESULT",
    "EVENT_USER",
    "MAX_TITLE_LENGTH",
    "ProjectMeta",
    "ROLE_TO_TYPE",
    "SESSION_SUFFIX",
    "SessionMeta",
    "append_compact_boundary",
    "append_context_collapse_span",
    "append_snip_boundary",
    "cleanup_expired_sessions",
    "clear_session",
    "ensure_message_id",
    "extract_title_from_events",
    "fork_session",
    "list_all_projects",
    "list_sessions",
    "load_context_collapse_state",
    "load_session",
    "load_session_with_report",
    "load_transcript",
    "parse_events",
    "project_dir",
    "project_dir_name",
    "read_existing_event_uuids",
    "read_last_event_uuid",
    "reconstruct_snipped_events",
    "reconstruct_snipped_events_with_report",
    "rename_session",
    "save_session",
    "session_file_path",
    "unwrap_message",
]
