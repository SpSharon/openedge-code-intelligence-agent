"""Event consumer over the agent — question events in, answers persisted.

The shape of an integration worker on an event pipeline, runnable with no
broker and no cloud account:

    source (queue semantics)  ->  worker  ->  agent.ask()  ->  store (SQLite)

    events.py   QuestionEvent + parse_event (a malformed event is a poison
                message: dead-lettered at once, never retried)
    source.py   EventSource protocol; MemorySource — an in-memory queue with
                SQS-style visibility timeout, redelivery and a receive-count
                redrive to dead letters; FileSource — a JSON Lines file
    store.py    AnswerStore — SQLite; idempotent on event_id (a redelivered
                event writes once); dead letters recorded with their reason
    worker.py   the loop: fetch -> parse -> idempotency check -> ask ->
                write -> ack. Commit after the write; transient failures
                retried with exponential backoff and jitter, bounded, then
                dead-lettered; one JSON log line per outcome with the event
                id, correlation id, attempt and duration
    run.py      python -m consumer.run --input events.jsonl --db answers.sqlite

    kafka_source.py   KafkaSource — the same four calls on a Kafka topic: a
                consumer group, one message in flight per partition, commit
                after the write, seek-and-pause on a retry, a dead-letter
                topic. kafka_run.py runs the worker against a broker;
                kafka_produce.py publishes a JSON Lines file (needs
                consumer/requirements-kafka.txt; the tests do not)

What it is not: an SQS client, or a hardened Kafka deployment. MemorySource mirrors SQS's
semantics (visibility timeout, at-least-once delivery, receive-count redrive)
so the worker's guarantees are tested without infrastructure; KafkaSource is
tested against fakes with the client's method names, and was run against one
local broker on 12 Nov 2026 (the three drills in the README).
"""
