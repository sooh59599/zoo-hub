CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$ BEGIN
CREATE TYPE event_status AS ENUM ('ACCEPTED', 'PROCESSING', 'DONE', 'FAILED');
EXCEPTION WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
CREATE TYPE job_status AS ENUM ('QUEUED', 'PROCESSING', 'SUCCEEDED', 'FAILED', 'DEAD');
EXCEPTION WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
CREATE TYPE action_kind AS ENUM ('EMAIL', 'WEBHOOK');
EXCEPTION WHEN duplicate_object THEN null;
END $$;

CREATE TABLE IF NOT EXISTS events (
                                      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source varchar(100) NOT NULL,
    type varchar(100) NOT NULL,
    subject_kind varchar(50) NOT NULL,
    subject_id varchar(200) NOT NULL,
    payload jsonb NOT NULL,
    occurred_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    idempotency_key varchar(300) UNIQUE,
    status event_status NOT NULL DEFAULT 'ACCEPTED'
    );

CREATE TABLE IF NOT EXISTS rules (
                                     id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name varchar(200) NOT NULL,
    enabled boolean NOT NULL DEFAULT true,
    match_source varchar(100),
    match_type varchar(100),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
    );

CREATE TABLE IF NOT EXISTS rule_actions (
                                            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_id uuid NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
    kind action_kind NOT NULL,
    config jsonb NOT NULL,
    order_no int NOT NULL DEFAULT 0
    );

CREATE TABLE IF NOT EXISTS jobs (
                                    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id uuid NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    rule_id uuid REFERENCES rules(id),
    action_id uuid REFERENCES rule_actions(id),
    kind action_kind NOT NULL,
    status job_status NOT NULL DEFAULT 'QUEUED',
    attempts int NOT NULL DEFAULT 0,
    max_attempts int NOT NULL DEFAULT 3,
    payload jsonb NOT NULL,
    last_error text,
    next_run_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
    );

CREATE TABLE IF NOT EXISTS job_attempts (
                                            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    attempt_no int NOT NULL,
    status varchar(20) NOT NULL,
    error text,
    result jsonb,
    started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz
    );

CREATE TABLE IF NOT EXISTS webhook_circuit (
                                               key varchar(300) PRIMARY KEY,
    state varchar(20) NOT NULL,
    failure_count int NOT NULL DEFAULT 0,
    opened_at timestamptz,
    last_failure_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
    );
