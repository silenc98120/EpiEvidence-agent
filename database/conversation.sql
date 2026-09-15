-- 会话历史表。可在已有 schema.sql 之后执行；不触碰 LangGraph checkpoint。
CREATE TABLE IF NOT EXISTS conversation (
    conversation_id UUID PRIMARY KEY,
    title TEXT NOT NULL CHECK (length(btrim(title)) > 0),
    preview TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'running', 'completed', 'failed', 'archived')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_conversation_updated ON conversation (updated_at DESC);

CREATE TABLE IF NOT EXISTS conversation_messages (
    message_id UUID PRIMARY KEY,
    conversation_id UUID NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
    task_id UUID REFERENCES tasks(task_id) ON DELETE SET NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'completed'
        CHECK (status IN ('streaming', 'completed', 'failed')),
    sequence_no INTEGER NOT NULL CHECK (sequence_no >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (conversation_id, sequence_no)
);

CREATE INDEX IF NOT EXISTS ix_conversation_messages_conversation_created
    ON conversation_messages (conversation_id, created_at);
